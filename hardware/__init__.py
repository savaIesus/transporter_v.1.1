"""Hardware adapters with lazy imports so diagnostics can load independently."""

__all__ = [
    "SerialTransport",
    "Axis",
    "Conveyor",
    "Distributor",
    "JogController",
    "find_controller",
    "list_available_ports",
]


def __getattr__(name):
    if name == "SerialTransport":
        from hardware.serial_transport import SerialTransport
        return SerialTransport
    if name == "Axis":
        from hardware.axis import Axis
        return Axis
    if name == "Conveyor":
        from hardware.conveyor import Conveyor
        return Conveyor
    if name == "Distributor":
        from hardware.distributor import Distributor
        return Distributor
    if name == "JogController":
        from hardware.jog_controller import JogController
        return JogController
    if name in {"find_controller", "list_available_ports"}:
        from hardware.port_discovery import find_controller, list_available_ports
        return {
            "find_controller": find_controller,
            "list_available_ports": list_available_ports,
        }[name]
    raise AttributeError(name)
