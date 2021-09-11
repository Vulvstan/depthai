import collections
import json
import math
import sys
import time
import traceback
from difflib import get_close_matches
from functools import partial
from pathlib import Path
from types import SimpleNamespace

import blobconverter
import cv2
import depthai as dai
import numpy as np

import enum
from depthai_helpers.utils import load_module, frame_norm, to_tensor_result


class BlobManager:
    def __init__(self, model_name=None, model_dir:Path=None):
        self.model_dir = None
        self.zoo_dir = None
        self.config_file = None
        self.blob_path = None
        self.use_zoo = False
        self.use_blob = False
        self.zoo_models = [f.stem for f in model_dir.parent.iterdir() if f.is_dir()] if model_dir is not None else []
        if model_dir is None:
            self.model_name = model_name
            self.use_zoo = True
        else:
            self.model_dir = Path(model_dir)
            self.zoo_dir = self.model_dir.parent
            self.model_name = model_name or self.model_dir.name
            self.config_file = self.model_dir / "model.yml"
            blob = next(self.model_dir.glob("*.blob"), None)
            if blob is not None:
                self.use_blob = True
                self.blob_path = blob
            if not self.config_file.exists():
                self.use_zoo = True

    def compile(self, shaves, openvino_version, target='auto'):
        version = openvino_version.name.replace("VERSION_", "").replace("_", ".")
        if self.use_blob:
            return self.blob_path
        elif self.use_zoo:
            try:
                self.blob_path = blobconverter.from_zoo(
                    name=self.model_name,
                    shaves=shaves,
                    version=version
                )
                return self.blob_path
            except Exception as e:
                if hasattr(e, 'response') and hasattr(e.response, 'status_code'):
                    if "not found in model zoo" in e.response.text:
                        all_models = set(self.zoo_models + blobconverter.zoo_list())
                        suggested = get_close_matches(self.model_name, all_models)
                        if len(suggested) > 0:
                            print("Model {} not found in model zoo. Did you mean: {} ?".format(self.model_name, " / ".join(suggested)), file=sys.stderr)
                        else:
                            print("Model {} not found in model zoo", file=sys.stderr)
                        raise SystemExit(1)
                    raise RuntimeError("Blob conversion failed with status {}! Error: \"{}\"".format(e.response.status_code, e.response.text))
                else:
                    raise
        else:
            self.blob_path = blobconverter.compile_blob(
                version=version,
                blob_name=self.model_name,
                req_data={
                    "name": self.model_name,
                    "use_zoo": True,
                },
                req_files={
                    'config': self.config_file,
                },
                data_type="FP16",
                shaves=shaves,
            )
            return self.blob_path


class PreviewDecoder:
    @staticmethod
    def nn_input(packet, manager=None):
        # if manager is not None and manager.lowBandwidth: TODO change once passthrough frame type (8) is supported by VideoEncoder
        if False:
            frame = cv2.imdecode(packet.getData(), cv2.IMREAD_COLOR)
        else:
            frame = packet.getCvFrame()
        if hasattr(manager, "nn_source") and manager.nn_source in (Previews.rectified_left.name, Previews.rectified_right.name):
            frame = cv2.flip(frame, 1)
        return frame

    @staticmethod
    def color(packet, manager=None):
        if manager is not None and manager.lowBandwidth and not manager.sync:  # TODO remove sync check once passthrough is supported for MJPEG encoding
            return cv2.imdecode(packet.getData(), cv2.IMREAD_COLOR)
        else:
            return packet.getCvFrame()

    @staticmethod
    def left(packet, manager=None):
        if manager is not None and manager.lowBandwidth and not manager.sync:  # TODO remove sync check once passthrough is supported for MJPEG encoding
            return cv2.imdecode(packet.getData(), cv2.IMREAD_GRAYSCALE)
        else:
            return packet.getCvFrame()

    @staticmethod
    def right(packet, manager=None):
        if manager is not None and manager.lowBandwidth and not manager.sync:  # TODO remove sync check once passthrough is supported for MJPEG encoding
            return cv2.imdecode(packet.getData(), cv2.IMREAD_GRAYSCALE)
        else:
            return packet.getCvFrame()

    @staticmethod
    def rectified_left(packet, manager=None):
        if manager is not None and manager.lowBandwidth and not manager.sync:  # TODO remove sync check once passthrough is supported for MJPEG encoding
            return cv2.flip(cv2.imdecode(packet.getData(), cv2.IMREAD_GRAYSCALE), 1)
        else:
            return cv2.flip(packet.getCvFrame(), 1)

    @staticmethod
    def rectified_right(packet, manager=None):
        if manager is not None and manager.lowBandwidth:  # TODO remove sync check once passthrough is supported for MJPEG encoding
            return cv2.flip(cv2.imdecode(packet.getData(), cv2.IMREAD_GRAYSCALE), 1)
        else:
            return cv2.flip(packet.getCvFrame(), 1)

    @staticmethod
    def depth_raw(packet, manager=None):
        # if manager is not None and manager.lowBandwidth:  TODO change once depth frame type (14) is supported by VideoEncoder
        if False:
            return cv2.imdecode(packet.getData(), cv2.IMREAD_UNCHANGED)
        else:
            return packet.getFrame()

    @staticmethod
    def depth(depth_raw, manager=None):
        dispScaleFactor = getattr(manager, "dispScaleFactor", None)
        if dispScaleFactor is None:
            baseline = getattr(manager, 'baseline', 75)  # mm
            fov = getattr(manager, 'fov', 71.86)
            focal = getattr(manager, 'focal', depth_raw.shape[1] / (2. * math.tan(math.radians(fov / 2))))
            dispScaleFactor = baseline * focal
            if manager is not None:
                setattr(manager, "dispScaleFactor", dispScaleFactor)

        with np.errstate(divide='ignore'):  # Should be safe to ignore div by zero here
            disp_frame = dispScaleFactor / depth_raw
        disp_frame = (disp_frame * manager.dispMultiplier).astype(np.uint8)
        return PreviewDecoder.disparity_color(disp_frame, manager)

    @staticmethod
    def disparity(packet, manager=None):
        if manager is not None and manager.lowBandwidth:
            raw_frame = cv2.imdecode(packet.getData(), cv2.IMREAD_GRAYSCALE)
        else:
            raw_frame = packet.getFrame()
        return (raw_frame*(manager.dispMultiplier if manager is not None else 255/95)).astype(np.uint8)

    @staticmethod
    def disparity_color(disparity, manager=None):
        return cv2.applyColorMap(disparity, manager.colorMap if manager is not None else cv2.COLORMAP_JET)


class Previews(enum.Enum):
    nn_input = partial(PreviewDecoder.nn_input)
    color = partial(PreviewDecoder.color)
    left = partial(PreviewDecoder.left)
    right = partial(PreviewDecoder.right)
    rectified_left = partial(PreviewDecoder.rectified_left)
    rectified_right = partial(PreviewDecoder.rectified_right)
    depth_raw = partial(PreviewDecoder.depth_raw)
    depth = partial(PreviewDecoder.depth)
    disparity = partial(PreviewDecoder.disparity)
    disparity_color = partial(PreviewDecoder.disparity_color)


class MouseClickTracker:
    def __init__(self):
        self.points = {}
        self.values = {}

    def select_point(self, name):
        def cb(event, x, y, flags, param):
            if event == cv2.EVENT_LBUTTONUP:
                if self.points.get(name) == (x, y):
                    del self.points[name]
                    if name in self.values:
                        del self.values[name]
                else:
                    self.points[name] = (x, y)
        return cb

    def extract_value(self, name, frame: np.ndarray):
        point = self.points.get(name, None)
        if point is not None:
            if name in (Previews.depth_raw.name, Previews.depth.name):
                self.values[name] = "{}mm".format(frame[point[1]][point[0]])
            elif name in (Previews.disparity_color.name, Previews.disparity.name):
                self.values[name] = "{}px".format(frame[point[1]][point[0]])
            elif len(frame.shape) == 3:
                self.values[name] = "R:{},G:{},B:{}".format(*frame[point[1]][point[0]][::-1])
            elif len(frame.shape) == 2:
                self.values[name] = "Gray:{}".format(frame[point[1]][point[0]])
            else:
                self.values[name] = str(frame[point[1]][point[0]])


class PreviewManager:
    def __init__(self, fps, display, nn_source, colorMap=cv2.COLORMAP_JET, dispMultiplier=255/95, mouseTracker=False, lowBandwidth=False, scale=None, sync=False):
        self.display = display
        self.frames = {}
        self.raw_frames = {}
        self.fps = fps
        self.nn_source = nn_source
        self.colorMap = colorMap
        self.lowBandwidth = lowBandwidth
        self.dispMultiplier = dispMultiplier
        self.mouse_tracker = MouseClickTracker() if mouseTracker else None
        self.scale = scale
        self.sync = sync

    def create_queues(self, device, callback=lambda *a, **k: None):
        if dai.CameraBoardSocket.LEFT in device.getConnectedCameras():
            calib = device.readCalibration()
            eeprom = calib.getEepromData()
            left_cam = calib.getStereoLeftCameraId()
            if left_cam != dai.CameraBoardSocket.AUTO:
                cam_info = eeprom.cameraData[left_cam]
                self.baseline = abs(cam_info.extrinsics.specTranslation.x * 10)  # cm -> mm
                self.fov = calib.getFov(calib.getStereoLeftCameraId())
                self.focal = (cam_info.width / 2) / (2. * math.tan(math.radians(self.fov / 2)))
            else:
                print("Warning: calibration data missing, using OAK-D defaults")
                self.baseline = 75
                self.fov = 71.86
                self.focal = 440
            self.dispScaleFactor = self.baseline * self.focal
        self.output_queues = []
        for name in self.display:
            cv2.namedWindow(name)
            callback(name)
            if self.mouse_tracker is not None:
                cv2.setMouseCallback(name, self.mouse_tracker.select_point(name))
            if name not in (Previews.disparity_color.name, Previews.depth.name):  # generated on host
                self.output_queues.append(device.getOutputQueue(name=name, maxSize=1, blocking=False))

        if Previews.disparity_color.name in self.display and Previews.disparity.name not in self.display:
            self.output_queues.append(device.getOutputQueue(name=Previews.disparity.name, maxSize=1, blocking=False))
        if Previews.depth.name in self.display and Previews.depth_raw.name not in self.display:
            self.output_queues.append(device.getOutputQueue(name=Previews.depth_raw.name, maxSize=1, blocking=False))

    def prepare_frames(self, callback):
        for queue in self.output_queues:
            packet = queue.tryGet()
            if packet is not None:
                self.fps.tick(queue.getName())
                frame = getattr(Previews, queue.getName()).value(packet, self)
                if frame is None:
                    print("[WARNING] Conversion of the {} frame has failed! (None value detected)".format(queue.getName()))
                    continue
                if self.scale is not None and queue.getName() in self.scale:
                    h, w = frame.shape[0:2]
                    frame = cv2.resize(frame, (int(w * self.scale[queue.getName()]), int(h * self.scale[queue.getName()])), interpolation=cv2.INTER_AREA)
                if queue.getName() in self.display:
                    callback(frame, queue.getName())
                    self.raw_frames[queue.getName()] = frame
                if self.mouse_tracker is not None:
                    if queue.getName() == Previews.disparity.name:
                        raw_frame = packet.getFrame() if not self.lowBandwidth else cv2.imdecode(packet.getData(), cv2.IMREAD_GRAYSCALE)
                        self.mouse_tracker.extract_value(Previews.disparity.name, raw_frame)
                        self.mouse_tracker.extract_value(Previews.disparity_color.name, raw_frame)
                    if queue.getName() == Previews.depth_raw.name:
                        raw_frame = packet.getFrame()  # if not self.lowBandwidth else cv2.imdecode(packet.getData(), cv2.IMREAD_UNCHANGED) TODO uncomment once depth encoding is possible
                        self.mouse_tracker.extract_value(Previews.depth_raw.name, raw_frame)
                        self.mouse_tracker.extract_value(Previews.depth.name, raw_frame)
                    else:
                        self.mouse_tracker.extract_value(queue.getName(), frame)

                if queue.getName() == Previews.disparity.name and Previews.disparity_color.name in self.display:
                    self.fps.tick(Previews.disparity_color.name)
                    self.raw_frames[Previews.disparity_color.name] = Previews.disparity_color.value(frame, self)

                if queue.getName() == Previews.depth_raw.name and Previews.depth.name in self.display:
                    self.fps.tick(Previews.depth.name)
                    self.raw_frames[Previews.depth.name] = Previews.depth.value(frame, self)

            for name in self.raw_frames:
                new_frame = self.raw_frames[name].copy()
                if name == Previews.depth_raw.name:
                    new_frame = cv2.normalize(new_frame, None, 255, 0, cv2.NORM_INF, cv2.CV_8UC1)
                self.frames[name] = new_frame

    def show_frames(self, callback=lambda *a, **k: None):
        for name, frame in self.frames.items():
            if self.mouse_tracker is not None:
                point = self.mouse_tracker.points.get(name)
                value = self.mouse_tracker.values.get(name)
                if point is not None:
                    cv2.circle(frame, point, 3, (255, 255, 255), -1)
                    cv2.putText(frame, str(value), (point[0] + 5, point[1] + 5), cv2.FONT_HERSHEY_TRIPLEX, 0.5, (0, 0, 0), 4, cv2.LINE_AA)
                    cv2.putText(frame, str(value), (point[0] + 5, point[1] + 5), cv2.FONT_HERSHEY_TRIPLEX, 0.5, (255, 255, 255), 1, cv2.LINE_AA)
            return_frame = callback(frame, name)  # Can be None, can be other frame e.g. after copy()
            cv2.imshow(name, return_frame if return_frame is not None else frame)

    def has(self, name):
        return name in self.frames

    def get(self, name):
        return self.frames.get(name, None)


class NNetManager:
    source_choices = ("color", "left", "right", "rectified_left", "rectified_right", "host")
    flip_detection = False
    full_fov = False
    config = None
    nn_family = None
    handler = None
    labels = None
    input_size = None
    confidence = None
    metadata = None
    openvino_version = None
    output_format = "raw"
    blob_path = None
    source = None
    count_label = None
    text_bg_color = (0, 0, 0)
    text_color = (255, 255, 255)
    line_type = cv2.LINE_AA
    text_type = cv2.FONT_HERSHEY_SIMPLEX
    bbox_color = np.random.random(size=(256, 3)) * 256  # Random Colors for bounding boxes

    def __init__(self, input_size, model_dir=None, model_name=None):

        self.input_size = input_size
        self.model_name = model_name
        self.model_dir = model_dir
        self.output_name = f"{self.model_name}_out"
        self.input_name = f"{self.model_name}_in"
        self.blob_manager = BlobManager(model_dir=self.model_dir, model_name=self.model_name)
        # Disaply depth roi bounding boxes

        if model_dir is not None:
            config_path = self.model_dir / Path(self.model_name).with_suffix(f".json")
            if config_path.exists():
                with config_path.open() as f:
                    self.config = json.load(f)
                    if "openvino_version" in self.config:
                        self.openvino_version =getattr(dai.OpenVINO.Version, 'VERSION_' + self.config.get("openvino_version"))
                    nn_config = self.config.get("nn_config", {})
                    self.labels = self.config.get("mappings", {}).get("labels", None)
                    self.nn_family = nn_config.get("NN_family", None)
                    self.output_format = nn_config.get("output_format", "raw")
                    self.metadata = nn_config.get("NN_specific_metadata", {})
                    if "input_size" in nn_config:
                        self.input_size = tuple(map(int, nn_config.get("input_size").split('x')))

                    self.confidence = self.metadata.get("confidence_threshold", nn_config.get("confidence_threshold", None))
                    if 'handler' in self.config:
                        self.handler = load_module(config_path.parent / self.config["handler"])

                        if not callable(getattr(self.handler, "draw", None)) or not callable(getattr(self.handler, "decode", None)):
                            raise RuntimeError("Custom model handler does not contain 'draw' or 'decode' methods!")

        if self.input_size is None:
            raise RuntimeError("Unable to determine the nn input size. Please use --cnn_input_size flag to specify it in WxH format: -nn-size <width>x<height>")

    def normFrame(self, frame):
        if not self.full_fov:
            scale_f = frame.shape[0] / self.input_size[1]
            return np.zeros((int(self.input_size[1] * scale_f), int(self.input_size[0] * scale_f)))
        else:
            return frame

    def cropOffsetX(self, frame):
        if not self.full_fov:
            cropped_w = (frame.shape[0] / self.input_size[1]) * self.input_size[0]
            return int((frame.shape[1] - cropped_w) // 2)
        else:
            return 0

    def create_nn_pipeline(self, p, nodes, source, flip_detection=False, shaves=6, use_depth=False, use_sbb=False, minDepth=100, maxDepth=10000, sbbScaleFactor=0.3, full_fov=False):
        if source not in self.source_choices:
            raise RuntimeError(f"Source {source} is invalid, available {self.source_choices}")
        self.source = source
        self.flip_detection = flip_detection
        self.full_fov = full_fov
        self.sbb = use_sbb
        if self.nn_family == "mobilenet":
            nn = p.createMobileNetSpatialDetectionNetwork() if use_depth else p.createMobileNetDetectionNetwork()
            nn.setConfidenceThreshold(self.confidence)
        elif self.nn_family == "YOLO":
            nn = p.createYoloSpatialDetectionNetwork() if use_depth else p.createYoloDetectionNetwork()
            nn.setConfidenceThreshold(self.confidence)
            nn.setNumClasses(self.metadata["classes"])
            nn.setCoordinateSize(self.metadata["coordinates"])
            nn.setAnchors(self.metadata["anchors"])
            nn.setAnchorMasks(self.metadata["anchor_masks"])
            nn.setIouThreshold(self.metadata["iou_threshold"])
        else:
            # TODO use createSpatialLocationCalculator
            nn = p.createNeuralNetwork()

        self.blob_path = self.blob_manager.compile(shaves, self.openvino_version)
        nn.setBlobPath(str(self.blob_path))
        nn.setNumInferenceThreads(2)
        nn.input.setBlocking(False)
        nn.input.setQueueSize(2)

        xout = p.createXLinkOut()
        xout.setStreamName(self.output_name)
        nn.out.link(xout.input)
        setattr(nodes, self.model_name, nn)
        setattr(nodes, self.output_name, xout)

        if self.source == "color":
            nodes.cam_rgb.preview.link(nn.input)
        elif self.source == "host":
            xin = p.createXLinkIn()
            xin.setStreamName(self.input_name)
            xin.out.link(nn.input)
            setattr(nodes, self.input_name, xin)
        elif self.source in ("left", "right", "rectified_left", "rectified_right"):
            nodes.manip = p.createImageManip()
            nodes.manip.initialConfig.setResize(*self.input_size)
            # The NN model expects BGR input. By default ImageManip output type would be same as input (gray in this case)
            nodes.manip.initialConfig.setFrameType(dai.RawImgFrame.Type.BGR888p)
            nodes.manip.setKeepAspectRatio(not self.full_fov)
            # NN inputs
            nodes.manip.out.link(nn.input)

            if self.source == "left":
                nodes.mono_left.out.link(nodes.manip.inputImage)
            elif self.source == "right":
                nodes.mono_right.out.link(nodes.manip.inputImage)
            elif self.source == "rectified_left":
                nodes.stereo.rectifiedLeft.link(nodes.manip.inputImage)
            elif self.source == "rectified_right":
                nodes.stereo.rectifiedRight.link(nodes.manip.inputImage)

        if self.nn_family in ("YOLO", "mobilenet") and use_depth:
            nodes.stereo.depth.link(nn.inputDepth)
            nn.setDepthLowerThreshold(minDepth)
            nn.setDepthUpperThreshold(maxDepth)
            nn.setBoundingBoxScaleFactor(sbbScaleFactor)

        return nn

    def get_label_text(self, label):
        if self.config is None or self.labels is None:
            return label
        elif int(label) < len(self.labels):
            return self.labels[int(label)]
        else:
            print(f"Label of ouf bounds (label_index: {label}, available_labels: {len(self.labels)}")
            return str(label)

    def decode(self, in_nn):
        if self.output_format == "detection":
            detections = in_nn.detections
            if self.flip_detection:
                for detection in detections:
                    # Since rectified frames are horizontally flipped by default
                    swap = detection.xmin
                    detection.xmin = 1 - detection.xmax
                    detection.xmax = 1 - swap
            return detections
        elif self.output_format == "raw":
            if self.handler is not None:
                return self.handler.decode(self, in_nn)
            else:
                try:
                    data = to_tensor_result(in_nn)
                    print("Received NN packet: ", ", ".join([f"{key}: {value.shape}" for key, value in data.items()]))
                except Exception as ex:
                    print("Received NN packet: <Preview unabailable: {}>".format(ex))
        else:
            raise RuntimeError("Unknown output format: {}".format(self.output_format))

    def draw_count(self, source, decoded_data):
        def draw_cnt(frame, cnt):
            cv2.putText(frame, f"{self.count_label}: {cnt}", (5, 46), self.text_type, 0.5, self.text_bg_color, 4, self.line_type)
            cv2.putText(frame, f"{self.count_label}: {cnt}", (5, 46), self.text_type, 0.5, self.text_color, 1, self.line_type)

        # Count the number of detected objects
        cnt_list = list(filter(lambda x: self.get_label_text(x.label) == self.count_label, decoded_data))
        if isinstance(source, PreviewManager):
            for frame in source.frames.values():
                draw_cnt(frame, len(cnt_list))
        else:
            draw_cnt(source, len(cnt_list))

    def draw(self, source, decoded_data):
        if self.output_format == "detection":
            def draw_detection(frame, detection):
                bbox = frame_norm(self.normFrame(frame), [detection.xmin, detection.ymin, detection.xmax, detection.ymax])
                if self.source == Previews.color.name and not self.full_fov:
                    bbox[::2] += self.cropOffsetX(frame)
                cv2.rectangle(frame, (bbox[0], bbox[1]), (bbox[2], bbox[3]), self.bbox_color[detection.label], 2)
                cv2.rectangle(frame, (bbox[0], (bbox[1] - 28)), ((bbox[0] + 110), bbox[1]), self.bbox_color[detection.label], cv2.FILLED)
                cv2.putText(frame, self.get_label_text(detection.label), (bbox[0] + 5, bbox[1] - 10),
                            self.text_type, 0.5, (0, 0, 0), 1, self.line_type)
                cv2.putText(frame, f"{int(detection.confidence * 100)}%", (bbox[0] + 62, bbox[1] - 10),
                            self.text_type, 0.5, (0, 0, 0), 1, self.line_type)

                if hasattr(detection, 'spatialCoordinates'):  # Display spatial coordinates as well
                    x_meters = detection.spatialCoordinates.x / 1000
                    y_meters = detection.spatialCoordinates.y / 1000
                    z_meters = detection.spatialCoordinates.z / 1000
                    cv2.putText(frame, "X: {:.2f} m".format(x_meters), (bbox[0] + 10, bbox[1] + 60),
                                self.text_type, 0.5, self.text_bg_color, 4, self.line_type)
                    cv2.putText(frame, "X: {:.2f} m".format(x_meters), (bbox[0] + 10, bbox[1] + 60),
                                self.text_type, 0.5, self.text_color, 1, self.line_type)
                    cv2.putText(frame, "Y: {:.2f} m".format(y_meters), (bbox[0] + 10, bbox[1] + 75),
                                self.text_type, 0.5, self.text_bg_color, 4, self.line_type)
                    cv2.putText(frame, "Y: {:.2f} m".format(y_meters), (bbox[0] + 10, bbox[1] + 75),
                                self.text_type, 0.5, self.text_color, 1, self.line_type)
                    cv2.putText(frame, "Z: {:.2f} m".format(z_meters), (bbox[0] + 10, bbox[1] + 90),
                                self.text_type, 0.5, self.text_bg_color, 4, self.line_type)
                    cv2.putText(frame, "Z: {:.2f} m".format(z_meters), (bbox[0] + 10, bbox[1] + 90),
                                self.text_type, 0.5, self.text_color, 1, self.line_type)
            for detection in decoded_data:
                if isinstance(source, PreviewManager):
                    for name, frame in source.frames.items():
                        draw_detection(frame, detection)
                else:
                    draw_detection(source, detection)

            if self.count_label is not None:
                self.draw_count(source, decoded_data)

        elif self.output_format == "raw" and self.handler is not None:
            if isinstance(source, PreviewManager):
                frames = list(source.frames.items())
            else:
                frames = [("host", source)]
            self.handler.draw(self, decoded_data, frames)


class FPSHandler:
    fps_bg_color = (0, 0, 0)
    fps_color = (255, 255, 255)
    fps_type = cv2.FONT_HERSHEY_SIMPLEX
    fps_line_type = cv2.LINE_AA

    def __init__(self, cap=None):
        self.timestamp = None
        self.start = None
        self.framerate = cap.get(cv2.CAP_PROP_FPS) if cap is not None else None
        self.useCamera = cap is None

        self.iter_cnt = 0
        self.ticks = {}

    def next_iter(self):
        if self.start is None:
            self.start = time.monotonic()

        if not self.useCamera and self.timestamp is not None:
            frame_delay = 1.0 / self.framerate
            delay = (self.timestamp + frame_delay) - time.monotonic()
            if delay > 0:
                time.sleep(delay)
        self.timestamp = time.monotonic()
        self.iter_cnt += 1

    def tick(self, name):
        if name not in self.ticks:
            self.ticks[name] = collections.deque(maxlen=100)
        self.ticks[name].append(time.monotonic())

    def tick_fps(self, name):
        if name in self.ticks and len(self.ticks[name]) > 1:
            time_diff = self.ticks[name][-1] - self.ticks[name][0]
            return (len(self.ticks[name]) - 1) / time_diff if time_diff != 0 else 0
        else:
            return 0

    def fps(self):
        if self.start is None or self.timestamp is None:
            return 0
        time_diff = self.timestamp - self.start
        return self.iter_cnt / time_diff if time_diff != 0 else 0

    def print_status(self):
        print("=== TOTAL FPS ===")
        for name in self.ticks:
            print(f"[{name}]: {self.tick_fps(name):.1f}")

    def draw_fps(self, frame, name):
        frame_fps = f"{name.upper()} FPS: {round(self.tick_fps(name), 1)}"
        # cv2.rectangle(frame, (0, 0), (120, 35), (255, 255, 255), cv2.FILLED)
        cv2.putText(frame, frame_fps, (5, 15), self.fps_type, 0.5, self.fps_bg_color, 4, self.fps_line_type)
        cv2.putText(frame, frame_fps, (5, 15), self.fps_type, 0.5, self.fps_color, 1, self.fps_line_type)

        if "nn" in self.ticks:
            cv2.putText(frame, f"NN FPS:  {round(self.tick_fps('nn'), 1)}", (5, 30), self.fps_type, 0.5, self.fps_bg_color, 4, self.fps_line_type)
            cv2.putText(frame, f"NN FPS:  {round(self.tick_fps('nn'), 1)}", (5, 30), self.fps_type, 0.5, self.fps_color, 1, self.fps_line_type)


class PipelineManager:
    def __init__(self, openvino_version=None, lowBandwidth=False):
        self.p = dai.Pipeline()
        self.openvino_version=openvino_version
        if openvino_version is not None:
            self.p.setOpenVINOVersion(openvino_version)
        self.nodes = SimpleNamespace()
        self.depthConfig = dai.StereoDepthConfig()
        self.lowBandwidth = lowBandwidth

    def set_nn_manager(self, nn_manager):
        self.nn_manager = nn_manager
        if self.openvino_version is None and self.nn_manager.openvino_version:
            self.p.setOpenVINOVersion(self.nn_manager.openvino_version)
        else:
            self.nn_manager.openvino_version = self.p.getOpenVINOVersion()

    def create_default_queues(self, device):
        for xout in filter(lambda node: isinstance(node, dai.node.XLinkOut), vars(self.nodes).values()):
            device.getOutputQueue(xout.getStreamName(), maxSize=1, blocking=False)
        for xin in filter(lambda node: isinstance(node, dai.node.XLinkIn), vars(self.nodes).values()):
            device.getInputQueue(xin.getStreamName(), maxSize=1, blocking=False)

    def mjpeg_link(self, node, xout, node_output):
        print("Creating MJPEG link for {} node and {} xlink stream...".format(node.getName(), xout.getStreamName()))
        videnc = self.p.createVideoEncoder()
        if isinstance(node, dai.node.ColorCamera) or isinstance(node, dai.node.MonoCamera):
            videnc.setDefaultProfilePreset(node.getResolutionWidth(), node.getResolutionHeight(), node.getFps(), dai.VideoEncoderProperties.Profile.MJPEG)
            node_output.link(videnc.input)
        elif isinstance(node, dai.node.StereoDepth):
            camera_node = getattr(self.nodes, 'mono_left', getattr(self.nodes, 'mono_right', None))
            if camera_node is None:
                raise RuntimeError("Unable to find mono camera node to determine frame size!")
            videnc.setDefaultProfilePreset(camera_node.getResolutionWidth(), camera_node.getResolutionHeight(), camera_node.getFps(), dai.VideoEncoderProperties.Profile.MJPEG)
            node_output.link(videnc.input)
        elif isinstance(node, dai.NeuralNetwork):
            w, h = self.nn_manager.input_size
            if w % 16 > 0:
                new_w = w - (w % 16)
                h = int((new_w / w) * h)
                w = int(new_w)
            if h % 2 > 0:
                h -= 1
            manip = self.p.createImageManip()
            manip.initialConfig.setResize(w, h)

            videnc.setDefaultProfilePreset(w, h, 30, dai.VideoEncoderProperties.Profile.MJPEG)
            node_output.link(manip.inputImage)
            manip.out.link(videnc.input)
        else:
            raise NotImplementedError("Unable to create mjpeg link for encountered node type: {}".format(type(node)))
        videnc.bitstream.link(xout.input)

    def create_color_cam(self, preview_size, res, fps, full_fov, orientation: dai.CameraImageOrientation=None, xout=False):
        # Define a source - color camera
        self.nodes.cam_rgb = self.p.createColorCamera()
        self.nodes.cam_rgb.setPreviewSize(*preview_size)
        self.nodes.cam_rgb.setInterleaved(False)
        self.nodes.cam_rgb.setResolution(res)
        self.nodes.cam_rgb.setFps(fps)
        if orientation is not None:
            self.nodes.cam_rgb.setImageOrientation(orientation)
        self.nodes.cam_rgb.setPreviewKeepAspectRatio(not full_fov)
        self.nodes.xout_rgb = self.p.createXLinkOut()
        self.nodes.xout_rgb.setStreamName(Previews.color.name)
        if xout:
            if self.lowBandwidth:
                self.mjpeg_link(self.nodes.cam_rgb, self.nodes.xout_rgb, self.nodes.cam_rgb.video)
            else:
                self.nodes.cam_rgb.video.link(self.nodes.xout_rgb.input)

    def create_depth(self, dct, median, sigma, lr, lrc_threshold, extended, subpixel, useDisparity=False, useDepth=False, useRectifiedLeft=False, useRectifiedRight=False):
        self.nodes.stereo = self.p.createStereoDepth()

        self.nodes.stereo.initialConfig.setConfidenceThreshold(dct)
        self.depthConfig.setConfidenceThreshold(dct)
        self.nodes.stereo.initialConfig.setMedianFilter(median)
        self.depthConfig.setMedianFilter(median)
        self.nodes.stereo.initialConfig.setBilateralFilterSigma(sigma)
        self.depthConfig.setBilateralFilterSigma(sigma)
        self.nodes.stereo.initialConfig.setLeftRightCheckThreshold(lrc_threshold)
        self.depthConfig.setLeftRightCheckThreshold(lrc_threshold)

        self.nodes.stereo.setLeftRightCheck(lr)
        self.nodes.stereo.setExtendedDisparity(extended)
        self.nodes.stereo.setSubpixel(subpixel)

        # Create mono left/right cameras if we haven't already
        if not hasattr(self.nodes, 'mono_left'):
            raise RuntimeError("Left mono camera not initialized. Call create_left_cam(res, fps) first!")
        if not hasattr(self.nodes, 'mono_right'):
            raise RuntimeError("Right mono camera not initialized. Call create_right_cam(res, fps) first!")

        self.nodes.mono_left.out.link(self.nodes.stereo.left)
        self.nodes.mono_right.out.link(self.nodes.stereo.right)

        self.nodes.xin_stereo_config = self.p.createXLinkIn()
        self.nodes.xin_stereo_config.setStreamName("stereo_config")
        self.nodes.xin_stereo_config.out.link(self.nodes.stereo.inputConfig)

        if useDepth:
            self.nodes.xout_depth = self.p.createXLinkOut()
            self.nodes.xout_depth.setStreamName(Previews.depth_raw.name)
            # if self.lowBandwidth:  TODO change once depth frame type (14) is supported by VideoEncoder
            if False:
                self.mjpeg_link(self.nodes.stereo, self.nodes.xout_depth, self.nodes.stereo.depth)
            else:
                self.nodes.stereo.depth.link(self.nodes.xout_depth.input)

        if useDisparity:
            self.nodes.xout_disparity = self.p.createXLinkOut()
            self.nodes.xout_disparity.setStreamName(Previews.disparity.name)
            if self.lowBandwidth:
                self.mjpeg_link(self.nodes.stereo, self.nodes.xout_disparity, self.nodes.stereo.disparity)
            else:
                self.nodes.stereo.disparity.link(self.nodes.xout_disparity.input)

        if useRectifiedLeft:
            self.nodes.xout_rect_left = self.p.createXLinkOut()
            self.nodes.xout_rect_left.setStreamName(Previews.rectified_left.name)
            if self.lowBandwidth:
                self.mjpeg_link(self.nodes.stereo, self.nodes.xout_rect_left, self.nodes.stereo.rectifiedLeft)
            else:
                self.nodes.stereo.rectifiedLeft.link(self.nodes.xout_rect_left.input)

        if useRectifiedRight:
            self.nodes.xout_rect_right = self.p.createXLinkOut()
            self.nodes.xout_rect_right.setStreamName(Previews.rectified_right.name)
            if self.lowBandwidth:
                self.mjpeg_link(self.nodes.stereo, self.nodes.xout_rect_right, self.nodes.stereo.rectifiedRight)
            else:
                self.nodes.stereo.rectifiedRight.link(self.nodes.xout_rect_right.input)

    def update_depth_config(self, device, dct=None, sigma=None, median=None, lrc_threshold=None):
        if dct is not None:
            self.depthConfig.setConfidenceThreshold(dct)
        if sigma is not None:
            self.depthConfig.setBilateralFilterSigma(sigma)
        if median is not None:
            self.depthConfig.setMedianFilter(median)
        if lrc_threshold is not None:
            self.depthConfig.setLeftRightCheckThreshold(lrc_threshold)

        device.getInputQueue("stereo_config").send(self.depthConfig)


    def create_left_cam(self, res, fps, orientation: dai.CameraImageOrientation=None, xout=False):
        self.nodes.mono_left = self.p.createMonoCamera()
        self.nodes.mono_left.setBoardSocket(dai.CameraBoardSocket.LEFT)
        self.nodes.mono_left.setResolution(res)
        self.nodes.mono_left.setFps(fps)
        if orientation is not None:
            self.nodes.mono_left.setImageOrientation(orientation)

        self.nodes.xout_left = self.p.createXLinkOut()
        self.nodes.xout_left.setStreamName(Previews.left.name)
        if xout:
            if self.lowBandwidth:
                self.mjpeg_link(self.nodes.mono_left, self.nodes.xout_left, self.nodes.mono_left.out)
            else:
                self.nodes.mono_left.out.link(self.nodes.xout_left.input)

    def create_right_cam(self, res, fps, orientation: dai.CameraImageOrientation=None, xout=False):
        self.nodes.mono_right = self.p.createMonoCamera()
        self.nodes.mono_right.setBoardSocket(dai.CameraBoardSocket.RIGHT)
        self.nodes.mono_right.setResolution(res)
        self.nodes.mono_right.setFps(fps)
        if orientation is not None:
            self.nodes.mono_right.setImageOrientation(orientation)

        self.nodes.xout_right = self.p.createXLinkOut()
        self.nodes.xout_right.setStreamName(Previews.right.name)
        if xout:
            if self.lowBandwidth:
                self.mjpeg_link(self.nodes.mono_right, self.nodes.xout_right, self.nodes.mono_right.out)
            else:
                self.nodes.mono_right.out.link(self.nodes.xout_right.input)

    def create_nn(self, nn, sync, use_depth=False, xout_nn_input=False, xout_sbb=False):
        # TODO adjust this function once passthrough frame type (8) is supported by VideoEncoder (for self.mjpeg_link)
        if xout_nn_input or (sync and self.nn_manager.source == "host"):
            self.nodes.xout_nn_input = self.p.createXLinkOut()
            self.nodes.xout_nn_input.setStreamName(Previews.nn_input.name)
            nn.passthrough.link(self.nodes.xout_nn_input.input)

        if xout_sbb and self.nn_manager.nn_family in ("YOLO", "mobilenet"):
            self.nodes.xout_sbb = self.p.createXLinkOut()
            self.nodes.xout_sbb.setStreamName("sbb")
            nn.boundingBoxMapping.link(self.nodes.xout_sbb.input)

        if sync:
            if self.nn_manager.source == "color":
                if not hasattr(self.nodes, "xout_rgb"):
                    self.nodes.xout_rgb = self.p.createXLinkOut()
                    self.nodes.xout_rgb.setStreamName(Previews.color.name)
                nn.passthrough.link(self.nodes.xout_rgb.input)
            elif self.nn_manager.source == "left":
                if not hasattr(self.nodes, "xout_left"):
                    self.nodes.xout_left = self.p.createXLinkOut()
                    self.nodes.xout_left.setStreamName(Previews.left.name)
                nn.passthrough.link(self.nodes.xout_left.input)
            elif self.nn_manager.source == "right":
                if not hasattr(self.nodes, "xout_right"):
                    self.nodes.xout_right = self.p.createXLinkOut()
                    self.nodes.xout_right.setStreamName(Previews.right.name)
                nn.passthrough.link(self.nodes.xout_right.input)
            elif self.nn_manager.source == "rectified_left":
                if not hasattr(self.nodes, "xout_rect_left"):
                    self.nodes.xout_rect_left = self.p.createXLinkOut()
                    self.nodes.xout_rect_left.setStreamName(Previews.rectified_left.name)
                nn.passthrough.link(self.nodes.xout_rect_left.input)
            elif self.nn_manager.source == "rectified_right":
                if not hasattr(self.nodes, "xout_rect_right"):
                    self.nodes.xout_rect_right = self.p.createXLinkOut()
                    self.nodes.xout_rect_right.setStreamName(Previews.rectified_right.name)
                nn.passthrough.link(self.nodes.xout_rect_right.input)

            if self.nn_manager.nn_family in ("YOLO", "mobilenet") and use_depth:
                if not hasattr(self.nodes, "xout_depth"):
                    self.nodes.xout_depth = self.p.createXLinkOut()
                    self.nodes.xout_depth.setStreamName(Previews.depth.name)
                nn.passthroughDepth.link(self.nodes.xout_depth.input)

    def create_system_logger(self):
        self.nodes.system_logger = self.p.createSystemLogger()
        self.nodes.system_logger.setRate(1)
        self.nodes.xout_system_logger = self.p.createXLinkOut()
        self.nodes.xout_system_logger.setStreamName("system_logger")
        self.nodes.system_logger.out.link(self.nodes.xout_system_logger.input)

    def enableLowBandwidth(self):
        self.lowBandwidth = True

    def set_xlink_chunk_size(self, chunk_size):
        self.p.setXLinkChunkSize(chunk_size)



class EncodingManager:
    def __init__(self, pm, encode_config: dict, encode_output=None):
        self.encoding_queues = {}
        self.encoding_nodes = {}
        self.encoding_files = {}
        self.encode_config = encode_config
        self.encode_output = Path(encode_output) or Path(__file__).parent
        self.pm = pm
        for camera_name, enc_fps in self.encode_config.items():
            self.create_encoder(camera_name, enc_fps)
            self.encoding_nodes[camera_name] = getattr(pm.nodes, camera_name + "_enc")

    def create_encoder(self, camera_name, enc_fps):
        allowed_sources = [Previews.left.name, Previews.right.name, Previews.color.name]
        if camera_name not in allowed_sources:
            raise ValueError("Camera param invalid, received {}, available choices: {}".format(camera_name, allowed_sources))
        node_name = camera_name.lower() + '_enc'
        xout_name = node_name + "_xout"
        enc_profile = dai.VideoEncoderProperties.Profile.H264_MAIN

        if camera_name == Previews.color.name:
            if not hasattr(self.pm.nodes, 'cam_rgb'):
                raise RuntimeError("RGB camera not initialized. Call create_color_cam(res, fps) first!")
            enc_resolution = (self.pm.nodes.cam_rgb.getVideoWidth(), self.pm.nodes.cam_rgb.getVideoHeight())
            enc_profile = dai.VideoEncoderProperties.Profile.H265_MAIN
            enc_in = self.pm.nodes.cam_rgb.video

        elif camera_name == Previews.left.name:
            if not hasattr(self.pm.nodes, 'mono_left'):
                raise RuntimeError("Left mono camera not initialized. Call create_left_cam(res, fps) first!")
            enc_resolution = (self.pm.nodes.mono_left.getResolutionWidth(), self.pm.nodes.mono_left.getResolutionHeight())
            enc_in = self.pm.nodes.mono_left.out
        elif camera_name == Previews.right.name:
            if not hasattr(self.pm.nodes, 'mono_right'):
                raise RuntimeError("Right mono camera not initialized. Call create_right_cam(res, fps) first!")
            enc_resolution = (self.pm.nodes.mono_right.getResolutionWidth(), self.pm.nodes.mono_right.getResolutionHeight())
            enc_in = self.pm.nodes.mono_right.out
        else:
            raise NotImplementedError("Unable to create encoder for {]".format(camera_name))

        enc = self.pm.p.createVideoEncoder()
        enc.setDefaultProfilePreset(*enc_resolution, enc_fps, enc_profile)
        enc_in.link(enc.input)
        setattr(self.pm.nodes, node_name, enc)

        enc_xout = self.pm.p.createXLinkOut()
        enc.bitstream.link(enc_xout.input)
        enc_xout.setStreamName(xout_name)
        setattr(self.pm.nodes, xout_name, enc_xout)

    def create_default_queues(self, device):
        for camera_name, enc_fps in self.encode_config.items():
            self.encoding_queues[camera_name] = device.getOutputQueue(camera_name + "_enc_xout", maxSize=30, blocking=True)
            self.encoding_files[camera_name] = (self.encode_output / camera_name).with_suffix(
                    ".h265" if self.encoding_nodes[camera_name].getProfile() == dai.VideoEncoderProperties.Profile.H265_MAIN else ".h264"
                ).open('wb')

    def parse_queues(self):
        for name, queue in self.encoding_queues.items():
            while queue.has():
                queue.get().getData().tofile(self.encoding_files[name])

    def close(self):
        def print_manual():
            print("To view the encoded data, convert the stream file (.h264/.h265) into a video file (.mp4), using commands below:")
            cmd = "ffmpeg -framerate {} -i {} -c copy {}"
            for name, file in self.encoding_files.items():
                print(cmd.format(self.encoding_nodes[name].getFrameRate(), file.name, str(Path(file.name).with_suffix('.mp4'))))

        for name, file in self.encoding_files.items():
            file.close()
        try:
            import ffmpy3
            for name, file in self.encoding_files.items():
                fps = self.encoding_nodes[name].getFrameRate()
                out_name = str(Path(file.name).with_suffix('.mp4'))
                try:
                    ff = ffmpy3.FFmpeg(
                        inputs={file.name: "-y"},
                        outputs={out_name: "-c copy -framerate {}".format(fps)}
                    )
                    print("Running conversion command... [{}]".format(ff.cmd))
                    ff.run()
                except ffmpy3.FFExecutableNotFoundError:
                    print("FFMPEG executable not found!")
                    traceback.print_exc()
                    print_manual()
                except ffmpy3.FFRuntimeError:
                    print("FFMPEG runtime error!")
                    traceback.print_exc()
                    print_manual()
            print("Video conversion complete!")
            for name, file in self.encoding_files.items():
                print("Produced file: {}".format(str(Path(file.name).with_suffix('.mp4'))))
        except ImportError:
            print("Module ffmpy3 not fouund!")
            traceback.print_exc()
            print_manual()
        except:
            print("Unknown error!")
            traceback.print_exc()
            print_manual()