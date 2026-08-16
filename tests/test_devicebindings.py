from circuitgen.devicebindings import (
    device_pin_names_compatible,
    load_device_bindings,
)


def test_registry_loads_only_provenance_backed_exact_devices():
    bindings = load_device_bindings()
    assert "INTERFACE_USB:CH340K" in bindings
    assert "RF_MODULE:ESP32-WROOM-32E" in bindings
    assert "AMPLIFIER_AUDIO:LM386" in bindings


def test_aliases_are_scoped_to_device_and_physical_pin():
    assert device_pin_names_compatible(
        "RF_Module:ESP32-WROOM-32E", "3", "RESET", "EN"
    ) is True
    assert device_pin_names_compatible(
        "Interface_USB:CH340K", "4", "V3", "~{DTR}"
    ) is False
    assert device_pin_names_compatible(
        "Interface_USB:CH340K", "10", "V3_CAP", "V3"
    ) is True
    assert device_pin_names_compatible("Timer:NE555D", "4", "RESET", "RESET") is None
    assert device_pin_names_compatible(
        "Amplifier_Audio:LM386", "1", "GAIN1", "GAIN"
    ) is True
    assert device_pin_names_compatible(
        "Amplifier_Audio:LM386", "8", "GAIN2", "GAIN"
    ) is True
