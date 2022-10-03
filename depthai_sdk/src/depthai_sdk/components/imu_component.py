import depthai as dai

from .component import Component, XoutBase
from ..oak_outputs.xout import XoutIMU
from ..oak_outputs.xout_base import StreamXout


class IMUComponent(Component):
    node: dai.node.IMU

    def __init__(self, pipeline: dai.Pipeline):
        self.out = self.Out(self)

        super().__init__()
        self.node = pipeline.createIMU()
        self.config_imu()  # Default settings, component won't work without them

    def config_imu(self,
                   sensors: list[dai.IMUSensor] = None,
                   report_rate: int = 100,
                   batch_report_threshold: int = 1,
                   max_batch_reports: int = 10,
                   enable_firmware_update: bool = False) -> None:
        """
        Configure IMU node.

        Args:
            sensors: List of sensors to enable.
            report_rate: Report rate in Hz.
            batch_report_threshold: Number of reports to batch before sending them to the host.
            max_batch_reports: Maximum number of batched reports to send to the host.
            enable_firmware_update: Enable firmware update if true, disable otherwise.

        Returns: None
        """
        sensors = sensors or [dai.IMUSensor.ACCELEROMETER_RAW, dai.IMUSensor.GYROSCOPE_RAW]

        self.node.enableIMUSensor(sensors=sensors, reportRate=report_rate)
        self.node.setBatchReportThreshold(batchReportThreshold=batch_report_threshold)
        self.node.setMaxBatchReports(maxBatchReports=max_batch_reports)
        self.node.enableFirmwareUpdate(enable_firmware_update)

    def _update_device_info(self, pipeline: dai.Pipeline, device: dai.Device, version: dai.OpenVINO.Version):
        pass

    class Out:
        _comp: 'IMUComponent'

        def __init__(self, imuComponent: 'IMUComponent'):
            self._comp = imuComponent

        def main(self, pipeline: dai.Pipeline, device: dai.Device) -> XoutBase:
            """
            Default output. Uses either camera(), replay(), or encoded() depending on the component settings.
            """
            return self.text(pipeline, device)

        def text(self, pipeline: dai.Pipeline, device: dai.Device) -> XoutBase:
            out = self._comp.node.out
            out = StreamXout(self._comp.node.id, out)
            imu_out = XoutIMU(out)
            return self._comp._create_xout(pipeline, imu_out)

    out: Out
