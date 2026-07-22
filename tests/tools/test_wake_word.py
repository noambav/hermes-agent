"""Tests for tools.wake_word — the "Hey Hermes" hotword detector.

No live audio or network: the sounddevice import is faked, engines are stubbed,
and lazy-dep availability is monkeypatched. Covers config resolution, engine
dispatch, the requirements probe, the detector fire/cooldown loop, and the
process-wide singleton lifecycle.
"""

import os
import sys
import time
import types

import pytest

import tools.wake_word as ww


# ── Config helpers ───────────────────────────────────────────────────────


def test_config_defaults_and_clamping():
    assert ww._provider({}) == "openwakeword"
    assert ww._provider({"provider": "Porcupine"}) == "porcupine"
    assert ww._sensitivity({"sensitivity": 5}) == 1.0
    assert ww._sensitivity({"sensitivity": -1}) == 0.0
    assert ww._sensitivity({"sensitivity": "nope"}) == 0.5
    assert ww.wake_phrase({"phrase": "hey jarvis"}) == "hey jarvis"
    assert ww.wake_phrase({}) == "hey hermes"


def test_wake_surface_enabled_gate():
    # Disabled → never, regardless of surface.
    assert ww.wake_surface_enabled("cli", {"enabled": False, "surface": "cli"}) is False
    # auto → every surface.
    for s in ("cli", "tui", "gui"):
        assert ww.wake_surface_enabled(s, {"enabled": True, "surface": "auto"}) is True
    # Pinned surface → only that one.
    cfg = {"enabled": True, "surface": "tui"}
    assert ww.wake_surface_enabled("tui", cfg) is True
    assert ww.wake_surface_enabled("cli", cfg) is False
    assert ww.wake_surface_enabled("gui", cfg) is False
    # Missing/blank surface defaults to auto.
    assert ww.wake_surface_enabled("gui", {"enabled": True}) is True


def test_looks_like_path():
    assert ww._looks_like_path("models/hey_hermes.onnx")
    assert ww._looks_like_path("custom.ppn")
    assert not ww._looks_like_path("hey_jarvis")


def test_load_wake_word_config_is_a_dict_with_defaults():
    # Wired into DEFAULT_CONFIG, so a real load returns the section shape.
    cfg = ww.load_wake_word_config()
    assert isinstance(cfg, dict)
    assert cfg.get("enabled") is False
    assert cfg.get("provider") == "openwakeword"


def test_load_wake_word_config_guards_non_dict(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.load_config", lambda: {"wake_word": "oops"}
    )
    assert ww.load_wake_word_config() == {}


# ── Engine dispatch ──────────────────────────────────────────────────────


def test_build_engine_dispatch(monkeypatch):
    monkeypatch.setattr(ww, "_OpenWakeWordEngine", lambda cfg: "oww")
    monkeypatch.setattr(ww, "_PorcupineEngine", lambda cfg: "pv")
    assert ww._build_engine({"provider": "openwakeword"}) == "oww"
    assert ww._build_engine({"provider": "porcupine"}) == "pv"
    with pytest.raises(ValueError):
        ww._build_engine({"provider": "bogus"})


# ── Requirements probe ───────────────────────────────────────────────────


def test_requirements_openwakeword_available(monkeypatch):
    monkeypatch.setattr(ww, "_audio_available", lambda: True)
    monkeypatch.setattr("tools.lazy_deps.is_available", lambda f: True)
    r = ww.check_wake_word_requirements(
        {"provider": "openwakeword", "phrase": "hey hermes"}
    )
    assert r["available"] is True
    assert r["provider"] == "openwakeword"
    assert r["phrase"] == "hey hermes"


def test_requirements_porcupine_needs_access_key(monkeypatch):
    monkeypatch.delenv("PORCUPINE_ACCESS_KEY", raising=False)
    monkeypatch.setattr(ww, "_audio_available", lambda: True)
    monkeypatch.setattr("tools.lazy_deps.is_available", lambda f: True)
    r = ww.check_wake_word_requirements({"provider": "porcupine"})
    assert r["available"] is False
    assert r["access_key_set"] is False
    assert "PORCUPINE_ACCESS_KEY" in r["hint"]


def test_requirements_unavailable_without_audio(monkeypatch):
    monkeypatch.setattr(ww, "_audio_available", lambda: False)
    monkeypatch.setattr("tools.lazy_deps.is_available", lambda f: True)
    r = ww.check_wake_word_requirements({"provider": "openwakeword"})
    assert r["available"] is False
    assert r["audio_available"] is False


# ── openWakeWord engine (base-model fetch) ───────────────────────────────


def _install_fake_openwakeword(monkeypatch):
    """Swap in a fake ``openwakeword`` so the engine builds with no network.

    Returns a ``calls`` dict recording every ``download_models`` invocation.
    """
    calls = {"download": []}

    class _FakeModel:
        def __init__(self, wakeword_models, inference_framework="onnx"):
            self.wakeword_models = list(wakeword_models)
            self.models = {"hey_hermes": object()}

        def predict(self, frame):
            return {"hey_hermes": 0.0}

        def reset(self):
            pass

    oww = types.ModuleType("openwakeword")
    oww.utils = types.SimpleNamespace(
        download_models=lambda names=[]: calls["download"].append(list(names))
    )
    model_mod = types.ModuleType("openwakeword.model")
    model_mod.Model = _FakeModel

    monkeypatch.setitem(sys.modules, "openwakeword", oww)
    monkeypatch.setitem(sys.modules, "openwakeword.model", model_mod)
    monkeypatch.setattr("tools.lazy_deps.ensure", lambda *a, **k: None)
    return calls


def test_openwakeword_ensures_base_models_for_custom_path(monkeypatch):
    # Regression: a custom ``.onnx`` path used to skip download_models entirely,
    # so a fresh install crashed at load time on a missing melspectrogram.onnx.
    # The base feature models must be ensured for a custom path too.
    calls = _install_fake_openwakeword(monkeypatch)
    eng = ww._OpenWakeWordEngine(
        {"provider": "openwakeword", "openwakeword": {"model": "/models/hey_hermes.onnx"}}
    )
    assert calls["download"] == [["/models/hey_hermes.onnx"]]
    assert eng._labels == ["hey_hermes"]


def test_openwakeword_fetches_builtin_by_name(monkeypatch):
    calls = _install_fake_openwakeword(monkeypatch)
    ww._OpenWakeWordEngine({"provider": "openwakeword", "openwakeword": {"model": "hey_jarvis"}})
    assert calls["download"] == [["hey_jarvis"]]


def test_bundled_hey_hermes_model_ships_on_disk():
    # The "hey hermes" wake word works out of the box only if the model is
    # actually bundled. Both framework artifacts must exist and be non-trivial.
    for framework in ("onnx", "tflite"):
        path = ww._bundled_wakeword_path(framework)
        assert os.path.exists(path), path
        assert os.path.getsize(path) > 1024, path


@pytest.mark.parametrize("model_value", [None, "", "hey_hermes", "hey hermes", "HEY_HERMES"])
def test_openwakeword_default_resolves_to_bundled_model(monkeypatch, model_value):
    # The default (and any "hey_hermes" alias) must load the bundled file, not be
    # passed through as a bogus built-in name that openWakeWord can't resolve.
    calls = _install_fake_openwakeword(monkeypatch)
    sub = {} if model_value is None else {"model": model_value}
    ww._OpenWakeWordEngine({"provider": "openwakeword", "openwakeword": sub})
    (downloaded,) = calls["download"]
    assert downloaded == [ww._bundled_wakeword_path("onnx")]


def test_openwakeword_bundled_model_matches_framework(monkeypatch):
    calls = _install_fake_openwakeword(monkeypatch)
    ww._OpenWakeWordEngine(
        {"provider": "openwakeword", "openwakeword": {"inference_framework": "tflite"}}
    )
    (downloaded,) = calls["download"]
    assert downloaded == [ww._bundled_wakeword_path("tflite")]
    assert downloaded[0].endswith(".tflite")


# ── Detector loop ────────────────────────────────────────────────────────


class _FakeStream:
    """Always-readable input stream that yields trivial frames."""

    def __init__(self, **_kw):
        self.closed = False

    def start(self):
        pass

    def read(self, n):
        time.sleep(0.01)
        return [0] * n, False

    def stop(self):
        pass

    def close(self):
        self.closed = True


class _FakeEngine:
    frame_length = 4

    def __init__(self, fire=True):
        self._fire = fire
        self.closed = False
        self.resets = 0

    def process(self, frame):
        return self._fire

    def reset(self):
        self.resets += 1

    def close(self):
        self.closed = True


def _fake_audio(monkeypatch):
    fake_sd = types.SimpleNamespace(InputStream=lambda **kw: _FakeStream(**kw))
    monkeypatch.setattr(ww, "_import_audio", lambda: (fake_sd, None))


def test_detector_fires_once_under_cooldown(monkeypatch):
    _fake_audio(monkeypatch)
    calls = []
    eng = _FakeEngine(fire=True)
    det = ww.WakeWordDetector(eng, lambda: calls.append(1), cooldown=10.0)
    det.start()
    time.sleep(0.25)
    det.stop()
    assert len(calls) == 1  # high cooldown suppresses repeats
    assert eng.closed is True
    assert det.running is False


def test_detector_refires_after_cooldown(monkeypatch):
    _fake_audio(monkeypatch)
    calls = []
    det = ww.WakeWordDetector(_FakeEngine(fire=True), lambda: calls.append(1), cooldown=0.05)
    det.start()
    time.sleep(0.3)
    det.stop()
    assert len(calls) >= 2


def test_detector_no_fire_when_engine_quiet(monkeypatch):
    _fake_audio(monkeypatch)
    calls = []
    det = ww.WakeWordDetector(_FakeEngine(fire=False), lambda: calls.append(1))
    det.start()
    time.sleep(0.15)
    det.stop()
    assert calls == []


def test_detector_resets_engine_on_each_start(monkeypatch):
    # Clearing the engine buffer on (re)start is what stops a resume right after
    # a voice turn from re-firing on stale audio (the runaway wake loop).
    _fake_audio(monkeypatch)
    eng = _FakeEngine(fire=False)
    det = ww.WakeWordDetector(eng, lambda: None)
    det.start()
    time.sleep(0.05)
    det.pause()
    det.resume()
    time.sleep(0.05)
    det.stop()
    assert eng.resets >= 2  # initial start + resume


def test_detector_pause_resume(monkeypatch):
    _fake_audio(monkeypatch)
    det = ww.WakeWordDetector(_FakeEngine(fire=False), lambda: None)
    det.start()
    time.sleep(0.05)
    assert det.running is True
    det.pause()
    assert det.running is False
    det.resume()
    time.sleep(0.05)
    assert det.running is True
    det.stop()
    assert det.running is False


# ── Singleton lifecycle ──────────────────────────────────────────────────


def test_singleton_lifecycle(monkeypatch):
    _fake_audio(monkeypatch)
    monkeypatch.setattr(ww, "_build_engine", lambda cfg: _FakeEngine(fire=False))

    assert ww.is_listening() is False
    det = ww.start_listening(lambda: None, config={})
    time.sleep(0.05)
    assert ww.is_listening() is True

    # Re-entrant start returns the same detector and re-arms it.
    det2 = ww.start_listening(lambda: None, config={})
    assert det2 is det

    ww.pause_listening()
    assert ww.is_listening() is False
    ww.resume_listening()
    time.sleep(0.05)
    assert ww.is_listening() is True

    ww.stop_listening()
    assert ww.is_listening() is False
