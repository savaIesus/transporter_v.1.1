"""Контракт верхнеуровневого startup/callback/shutdown после декомпозиции."""

from __future__ import annotations

from types import SimpleNamespace
import unittest

from application.callbacks import ExitCoordinator, ThresholdCallbacks
from application.lifecycle import ProductionApplication
from application.runtime import RuntimeState
from application.shutdown import ShutdownManager
from application.startup import SystemInitializer
from application.ui import DesktopUI
from json_sender import scan_and_send_all_batches


class PassiveThread:
    """Поток для детерминированных тестов: target не запускается."""

    def __init__(self, target, daemon=True):
        self.target = target
        self.daemon = daemon
        self.started = False

    def start(self):
        self.started = True

    def is_alive(self):
        return False

    def join(self, timeout=None):
        return None


class ImmediateThread(PassiveThread):
    def start(self):
        self.started = True
        self.target()


class FailingStartThread(PassiveThread):
    def start(self):
        self.started = True
        raise RuntimeError("thread failed to start")


class StubbornThread:
    def __init__(self):
        self.join_timeouts = []

    def is_alive(self):
        return True

    def join(self, timeout=None):
        self.join_timeouts.append(timeout)


class HangingWorker:
    """Дубль потока финализации архива: target не запускается.

    Имитирует зависший сетевой диск: поток «вечно жив», join уходит
    по таймауту, освобождение ресурсов обязано продолжиться.
    """

    def __init__(self, target, daemon=True):
        self.target = target
        self.daemon = daemon

    def start(self):
        pass

    def is_alive(self):
        return True

    def join(self, timeout=None):
        return None


class FakeServer:
    def __init__(self, events):
        self.events = events
        self.boot_current = None
        self.archive = None
        self.archive_config_path = None
        self.thresholds = None
        self.threshold_labels = None
        self.thresholds_path = None

    def start_server(self, host, port):
        self.events.append(("server.start", host, port))


class FailingServer(FakeServer):
    def start_server(self, host, port):
        super().start_server(host, port)
        raise RuntimeError("server failed to start")


class FakeMonitor:
    def __init__(self, events):
        self.events = events
        self.server = FakeServer(events)
        self.host = "127.0.0.1"
        self.port = 8000
        self.closed = 0
        self.updates = []
        self.exit_callback = None

    def boot_step_start(self, key, message=None):
        self.server.boot_current = key
        self.events.append(("boot.start", key, message))

    def boot_step_done(self, key, message=None):
        self.events.append(("boot.done", key, message))

    def boot_step_error(self, key, message):
        self.events.append(("boot.error", key, message))

    def boot_complete(self):
        self.events.append("boot.complete")

    def set_camera_roles(self, mapping):
        self.events.append(("camera.roles", dict(mapping)))

    def update(self, **kwargs):
        self.updates.append(kwargs)

    def close_window(self):
        self.closed += 1

    def stop_server(self):
        self.events.append("server.stop")


class FakeTransport:
    def __init__(self, events):
        self.events = events

    def send(self, command):
        self.events.append(("serial.send", command))

    def close(self):
        self.events.append("serial.close")


class FakeCycle:
    def __init__(self, events):
        self.events = events
        self.state = "IDLE"
        self.force_exit_requested = False
        self.jog = SimpleNamespace(status={"busy": False})
        self.live = SimpleNamespace(
            stop=lambda: self.events.append("live.stop"),
            wait_for_roles=lambda roles, timeout: (),
            error=None,
        )

    def start(self):
        self.events.append("cycle.start")

    def request_start(self): pass
    def request_stop(self): pass
    def request_pause(self): pass
    def request_resume(self): pass
    def distributor_diagnostic(self): pass
    def diagnostic_check_cameras(self): pass
    def diagnostic_check_vision_rules(self): pass
    def diagnostic_analyze_selected_camera(self): pass
    def diagnostic_release_selected_camera(self): pass

    def enter_jog(self):
        self.events.append("jog.enter")
        return True

    def exit_jog(self): pass

    def jog_hold_start(self): pass
    def jog_hold_heartbeat(self): pass
    def jog_hold_release(self): pass

    def _refresh_monitor(self):
        self.events.append("monitor.refresh")

    def request_exit(self):
        self.events.append("cycle.exit")

    def request_force_exit(self):
        self.events.append("cycle.force_exit")
        self.force_exit_requested = True


class FakeFactory:
    def __init__(self, events):
        self.events = events
        self.transport = FakeTransport(events)
        self.cycle = FakeCycle(events)

    def load_calibration(self):
        self.events.append("calibration")
        return {"any": "calibration"}

    def create_cameras(self):
        self.events.append("cameras")
        return SimpleNamespace(mapping={"TOP": 6}, cameras=[object()])

    def create_vision(self):
        factory_events = self.events

        class Vision:
            models = {"model": object()}

            def warmup(self):
                factory_events.append("vision.warmup")

        self.events.append("vision")
        return Vision()

    def create_inspection(self, vision):
        self.events.append("inspection")
        decision = SimpleNamespace(rules=[object()], thresholds={"TOP.x": 1})
        inspector = SimpleNamespace(decision=decision)
        archive = SimpleNamespace()
        return SimpleNamespace(
            threshold_loader=SimpleNamespace(labels={"TOP.x": "X"}),
            thresholds={"TOP.x": 1},
            decision=decision,
            inspector=inspector,
            archive=archive,
        )

    def discover_controller(self, *, baudrate, preferred_port):
        self.events.append(("serial.discover", baudrate, preferred_port))
        return "COM7", "ok"

    def create_transport(self, *, port, baudrate):
        self.events.append(("serial.create", port, baudrate))
        return self.transport

    def create_hardware(self, transport, calibration, cancel_check):
        self.events.append("hardware")
        events = self.events

        class Distributor:
            dist1_open_position = 340
            dist2_cleanup_position = 340

            def initialize(self):
                events.append("distributor.initialize")

        return SimpleNamespace(
            conveyor=object(),
            distributor=Distributor(),
            jog=object(),
        )

    def create_cycle(self, **kwargs):
        self.events.append("cycle.create")
        return self.cycle


class ApplicationStartupTest(unittest.TestCase):
    def test_startup_keeps_stage_order_and_binds_cycle(self):
        events = []
        monitor = FakeMonitor(events)
        runtime = RuntimeState(monitor=monitor)
        factory = FakeFactory(events)
        exits = ExitCoordinator(runtime, thread_factory=ImmediateThread)
        initializer = SystemInitializer(
            runtime,
            factory,
            exits,
            thread_factory=PassiveThread,
            sleep=lambda _seconds: None,
        )

        initializer.run()

        started = [
            event[1]
            for event in events
            if isinstance(event, tuple) and event[0] == "boot.start"
        ]
        self.assertEqual(
            started,
            [
                "cameras", "models_load", "models_warm", "inspection",
                "serial", "hardware", "cycle", "ready",
            ],
        )
        self.assertLess(
            events.index("distributor.initialize"),
            events.index("cycle.create"),
        )
        self.assertIn(("serial.send", "G1"), events)
        self.assertIn(("serial.send", "G25"), events)
        self.assertIs(runtime.cycle, factory.cycle)
        self.assertTrue(runtime.cycle_thread.started)
        self.assertIn("jog.enter", events)
        callback_pairs = {
            "start_callback": "request_start",
            "stop_callback": "request_stop",
            "pause_callback": "request_pause",
            "resume_callback": "request_resume",
            "distributor_diagnostic_callback": "distributor_diagnostic",
            "camera_diagnostic_callback": "diagnostic_check_cameras",
            "vision_rule_diagnostic_callback": (
                "diagnostic_check_vision_rules"
            ),
            "selected_model_analysis_callback": (
                "diagnostic_analyze_selected_camera"
            ),
            "selected_model_release_callback": (
                "diagnostic_release_selected_camera"
            ),
            "jog_enter_callback": "enter_jog",
            "jog_exit_callback": "exit_jog",
            "jog_hold_start_callback": "jog_hold_start",
            "jog_hold_heartbeat_callback": "jog_hold_heartbeat",
            "jog_hold_release_callback": "jog_hold_release",
        }
        for monitor_name, cycle_name in callback_pairs.items():
            self.assertEqual(
                getattr(monitor, monitor_name),
                getattr(factory.cycle, cycle_name),
            )
        self.assertEqual(monitor.exit_callback, exits.request_exit)
        monitor.active_camera_callback("TOP")
        self.assertIn("monitor.refresh", events)
        self.assertEqual(monitor.server.thresholds, {"TOP.x": 1})
        self.assertEqual(len(monitor.updates), 1)
        self.assertIn("boot.complete", events)

    def test_startup_waits_for_first_frame_from_every_camera(self):
        events = []
        monitor = FakeMonitor(events)
        runtime = RuntimeState(monitor=monitor)
        factory = FakeFactory(events)
        factory.cycle.live.wait_for_roles = (
            lambda roles, timeout: ("TOP",)
        )
        exits = ExitCoordinator(runtime, thread_factory=ImmediateThread)
        initializer = SystemInitializer(
            runtime,
            factory,
            exits,
            thread_factory=PassiveThread,
            sleep=lambda _seconds: None,
            initial_camera_frames_timeout=0.0,
        )

        initializer.run()

        errors = [
            event for event in events
            if isinstance(event, tuple) and event[0] == "boot.error"
        ]
        self.assertTrue(errors)
        self.assertEqual(errors[-1][1], "ready")
        self.assertIn("Нет первого кадра: TOP", errors[-1][2])
        self.assertNotIn("boot.complete", events)

    def test_startup_failure_stops_before_serial_and_hardware(self):
        events = []
        monitor = FakeMonitor(events)
        runtime = RuntimeState(monitor=monitor)

        class FailingFactory(FakeFactory):
            def create_vision(self):
                self.events.append("vision")
                raise RuntimeError("model load failed")

        factory = FailingFactory(events)
        exits = ExitCoordinator(runtime, thread_factory=ImmediateThread)
        initializer = SystemInitializer(
            runtime,
            factory,
            exits,
            thread_factory=PassiveThread,
            sleep=lambda _seconds: None,
        )

        initializer.run()

        self.assertIsNotNone(runtime.cameras)
        self.assertIsNone(runtime.transport)
        self.assertIsNone(runtime.cycle)
        self.assertIn(
            ("boot.error", "models_load", "Ошибка загрузки моделей: model load failed"),
            events,
        )
        self.assertFalse(any(
            event == "hardware"
            or (
                isinstance(event, tuple)
                and event[0] == "serial.discover"
            )
            for event in events
        ))
        self.assertNotIn("boot.complete", events)


class ThresholdCallbacksTest(unittest.TestCase):
    def test_apply_validates_saves_and_rebuilds_decision(self):
        events = []
        monitor = FakeMonitor(events)
        monitor.server.threshold_labels = {"TOP.x": "Старое имя"}
        runtime = RuntimeState(monitor=monitor)
        runtime.cycle = SimpleNamespace(
            state="IDLE",
            jog=SimpleNamespace(status={"busy": False}),
        )
        inspector = SimpleNamespace(
            decision=SimpleNamespace(thresholds={"TOP.x": 1, "TOP.y": 2})
        )

        class Store:
            @staticmethod
            def validate(values):
                events.append(("validate", dict(values)))

            @staticmethod
            def save_file(path, values, labels=None):
                events.append(
                    ("save", path, dict(values), dict(labels or {}))
                )

        def make_decision(*, thresholds):
            events.append(("decision", dict(thresholds)))
            return SimpleNamespace(thresholds=dict(thresholds))

        callbacks = ThresholdCallbacks(
            runtime,
            inspector,
            threshold_store=Store,
            decision_factory=make_decision,
        )
        result = callbacks.apply(
            "TOP",
            {"x": 3},
            {"x": "Новое имя"},
        )

        self.assertEqual(result, {"TOP.x": 3, "TOP.y": 2})
        self.assertEqual(inspector.decision.thresholds, result)
        self.assertEqual(events[0], ("validate", result))
        self.assertEqual(
            events[1],
            (
                "save",
                "thresholds.json",
                result,
                {"TOP.x": "Новое имя"},
            ),
        )
        self.assertEqual(events[2], ("decision", result))

    def test_apply_is_blocked_while_cycle_is_running(self):
        monitor = FakeMonitor([])
        runtime = RuntimeState(monitor=monitor)
        runtime.cycle = SimpleNamespace(
            state="RUNNING",
            jog=SimpleNamespace(status={"busy": False}),
        )
        inspector = SimpleNamespace(
            decision=SimpleNamespace(thresholds={"TOP.x": 1})
        )
        callbacks = ThresholdCallbacks(
            runtime,
            inspector,
            threshold_store=SimpleNamespace(),
            decision_factory=lambda **_kwargs: None,
        )

        with self.assertRaisesRegex(RuntimeError, "только до пуска"):
            callbacks.apply("TOP", {"x": 2}, {})


class ExitAndShutdownTest(unittest.TestCase):
    def test_first_exit_is_graceful_and_second_is_forced(self):
        events = []
        monitor = FakeMonitor(events)
        runtime = RuntimeState(monitor=monitor)
        runtime.cycle = FakeCycle(events)
        exits = ExitCoordinator(runtime, thread_factory=ImmediateThread)

        exits.request_exit()
        exits.request_exit()

        self.assertTrue(runtime.shutdown_requested.is_set())
        self.assertEqual(events.count("cycle.exit"), 1)
        self.assertEqual(events.count("cycle.force_exit"), 1)
        self.assertEqual(monitor.closed, 2)

    def test_graceful_exit_keeps_window_open_until_cycle_finishes(self):
        events = []
        monitor = FakeMonitor(events)
        runtime = RuntimeState(monitor=monitor)
        runtime.cycle = FakeCycle(events)
        runtime.cycle_thread = StubbornThread()
        exits = ExitCoordinator(runtime, thread_factory=ImmediateThread)

        exits.request_exit()
        self.assertEqual(monitor.closed, 0)
        self.assertEqual(runtime.cycle_thread.join_timeouts, [135.0])

        exits.request_exit()
        self.assertEqual(monitor.closed, 1)
        self.assertEqual(runtime.cycle_thread.join_timeouts, [135.0, 15.0])

    def test_close_wait_thread_error_falls_back_to_direct_close(self):
        monitor = FakeMonitor([])
        runtime = RuntimeState(monitor=monitor)

        def failing_thread_factory(**_kwargs):
            raise RuntimeError("thread unavailable")

        exits = ExitCoordinator(
            runtime,
            thread_factory=failing_thread_factory,
        )
        exits.request_exit()

        self.assertEqual(monitor.closed, 1)

    def test_exit_during_startup_stops_controller_before_closing(self):
        events = []
        monitor = FakeMonitor(events)
        runtime = RuntimeState(monitor=monitor)
        runtime.transport = FakeTransport(events)
        exits = ExitCoordinator(runtime, thread_factory=ImmediateThread)

        exits.request_exit()

        self.assertEqual(
            [event for event in events if isinstance(event, tuple)],
            [("serial.send", "G1"), ("serial.send", "G25")],
        )
        self.assertEqual(monitor.closed, 1)

    def test_force_exit_error_sends_fallback_controller_stop(self):
        events = []
        monitor = FakeMonitor(events)
        runtime = RuntimeState(monitor=monitor)

        class FailingCycle(FakeCycle):
            state = "FAULT"

            def request_force_exit(self):
                raise RuntimeError("force exit failed")

        runtime.cycle = FailingCycle(events)
        runtime.cycle.state = "FAULT"
        runtime.transport = FakeTransport(events)
        exits = ExitCoordinator(runtime, thread_factory=ImmediateThread)

        exits.request_exit()

        self.assertIn(("serial.send", "G1"), events)
        self.assertIn(("serial.send", "G25"), events)
        self.assertEqual(monitor.closed, 1)

    def test_exit_callback_error_does_not_block_window_close(self):
        events = []
        monitor = FakeMonitor(events)
        runtime = RuntimeState(monitor=monitor)

        class FailingCycle(FakeCycle):
            def request_exit(self):
                raise RuntimeError("cycle exit failed")

        runtime.cycle = FailingCycle(events)
        exits = ExitCoordinator(runtime, thread_factory=ImmediateThread)

        exits.request_exit()

        self.assertEqual(monitor.closed, 1)

    def test_shutdown_releases_resources_in_safe_order(self):
        events = []
        monitor = FakeMonitor(events)
        runtime = RuntimeState(monitor=monitor)
        runtime.cycle = FakeCycle(events)
        runtime.cameras = SimpleNamespace(
            release=lambda: events.append("cameras.release")
        )
        runtime.transport = FakeTransport(events)
        runtime.archive = SimpleNamespace(compress=lambda: None)
        manager = ShutdownManager(runtime, thread_factory=ImmediateThread)

        manager.shutdown()

        self.assertIn("cycle.force_exit", events)
        self.assertLess(events.index("server.stop"), events.index("live.stop"))
        self.assertLess(events.index("live.stop"), events.index("cameras.release"))
        self.assertLess(events.index("cameras.release"), events.index("serial.close"))

    def test_cycle_force_exit_error_does_not_skip_resource_cleanup(self):
        events = []
        monitor = FakeMonitor(events)
        runtime = RuntimeState(monitor=monitor)

        class FailingCycle(FakeCycle):
            def request_force_exit(self):
                raise RuntimeError("force exit failed")

        runtime.cycle = FailingCycle(events)
        runtime.cameras = SimpleNamespace(
            release=lambda: events.append("cameras.release")
        )
        runtime.transport = FakeTransport(events)
        manager = ShutdownManager(runtime, thread_factory=ImmediateThread)

        manager.shutdown()

        self.assertIn("server.stop", events)
        self.assertIn("live.stop", events)
        self.assertIn("cameras.release", events)
        self.assertIn(("serial.send", "G1"), events)
        self.assertIn(("serial.send", "G25"), events)
        self.assertIn("serial.close", events)

    def test_archive_worker_error_does_not_skip_resource_cleanup(self):
        events = []
        monitor = FakeMonitor(events)
        runtime = RuntimeState(monitor=monitor)
        runtime.cycle = FakeCycle(events)
        runtime.cameras = SimpleNamespace(
            release=lambda: events.append("cameras.release")
        )
        runtime.transport = FakeTransport(events)
        runtime.archive = SimpleNamespace(compress=lambda: None)

        def failing_thread_factory(**_kwargs):
            raise RuntimeError("thread unavailable")

        manager = ShutdownManager(
            runtime,
            thread_factory=failing_thread_factory,
        )
        manager.shutdown()

        self.assertIn("cameras.release", events)
        self.assertIn("serial.close", events)

    def test_shutdown_compresses_archive_before_releasing_cameras(self):
        events = []
        monitor = FakeMonitor(events)
        runtime = RuntimeState(monitor=monitor)
        runtime.cycle = FakeCycle(events)
        runtime.cameras = SimpleNamespace(
            release=lambda: events.append("cameras.release")
        )
        runtime.archive = SimpleNamespace(
            compress=lambda: events.append("archive.compress"),
        )
        manager = ShutdownManager(runtime, thread_factory=ImmediateThread)

        manager.shutdown()

        self.assertIn("archive.compress", events)
        self.assertLess(
            events.index("archive.compress"), events.index("cameras.release"),
        )

    def test_shutdown_finalizes_archive_before_releasing_cameras(self):
        events = []
        monitor = FakeMonitor(events)
        runtime = RuntimeState(monitor=monitor)
        runtime.cycle = FakeCycle(events)
        runtime.cameras = SimpleNamespace(
            release=lambda: events.append("cameras.release")
        )
        runtime.transport = FakeTransport(events)
        sender_roots = []

        def batch_sender(root):
            sender_roots.append(root)
            events.append("json.send")

        runtime.archive = SimpleNamespace(
            root_folder="archive",
            close_manifest=lambda: events.append("manifest.close"),
            compress=lambda: events.append("archive.compress"),
        )
        manager = ShutdownManager(
            runtime,
            thread_factory=ImmediateThread,
            batch_sender=batch_sender,
        )

        manager.shutdown()

        # Отправка получает фактический корень архива из конфигурации.
        self.assertEqual(sender_roots, ["archive"])
        # Манифест закрывается до отправки, отправка -- до сжатия,
        # сжатие -- до освобождения камер.
        order = [events.index(name) for name in (
            "manifest.close", "json.send", "archive.compress",
            "cameras.release",
        )]
        self.assertEqual(order, sorted(order))

    def test_shutdown_sender_failure_does_not_skip_compression(self):
        events = []
        monitor = FakeMonitor(events)
        runtime = RuntimeState(monitor=monitor)
        runtime.cycle = FakeCycle(events)
        runtime.cameras = SimpleNamespace(
            release=lambda: events.append("cameras.release")
        )
        runtime.transport = FakeTransport(events)

        def failing_sender(_root):
            raise RuntimeError("network share hung")

        runtime.archive = SimpleNamespace(
            root_folder="archive",
            close_manifest=lambda: events.append("manifest.close"),
            compress=lambda: events.append("archive.compress"),
        )
        manager = ShutdownManager(
            runtime,
            thread_factory=ImmediateThread,
            batch_sender=failing_sender,
        )

        manager.shutdown()

        self.assertIn("manifest.close", events)
        self.assertIn("archive.compress", events)
        self.assertIn("cameras.release", events)
        self.assertIn("serial.close", events)

    def test_shutdown_close_manifest_failure_still_sends_json(self):
        events = []
        monitor = FakeMonitor(events)
        runtime = RuntimeState(monitor=monitor)
        runtime.cycle = FakeCycle(events)
        runtime.cameras = SimpleNamespace(
            release=lambda: events.append("cameras.release")
        )
        runtime.transport = FakeTransport(events)

        def failing_close():
            raise RuntimeError("disk full")

        runtime.archive = SimpleNamespace(
            root_folder="archive",
            close_manifest=failing_close,
            compress=lambda: events.append("archive.compress"),
        )
        manager = ShutdownManager(
            runtime,
            thread_factory=ImmediateThread,
            batch_sender=lambda _root: events.append("json.send"),
        )

        manager.shutdown()

        self.assertIn("json.send", events)
        self.assertIn("archive.compress", events)
        self.assertIn("cameras.release", events)

    def test_shutdown_duck_typed_archive_without_root_skips_sender(self):
        events = []
        monitor = FakeMonitor(events)
        runtime = RuntimeState(monitor=monitor)
        runtime.cycle = FakeCycle(events)
        runtime.cameras = SimpleNamespace(
            release=lambda: events.append("cameras.release")
        )
        runtime.transport = FakeTransport(events)
        runtime.archive = SimpleNamespace(
            compress=lambda: events.append("archive.compress"),
        )
        manager = ShutdownManager(
            runtime,
            thread_factory=ImmediateThread,
            batch_sender=lambda root: self.fail(
                f"отправка не должна запускаться без root_folder: {root}"
            ),
        )

        manager.shutdown()

        self.assertIn("archive.compress", events)
        self.assertIn("cameras.release", events)

    def test_shutdown_hanging_finalization_times_out(self):
        events = []
        monitor = FakeMonitor(events)
        runtime = RuntimeState(monitor=monitor)
        runtime.cycle = FakeCycle(events)
        runtime.cameras = SimpleNamespace(
            release=lambda: events.append("cameras.release")
        )
        runtime.transport = FakeTransport(events)
        runtime.archive = SimpleNamespace(
            root_folder="archive",
            close_manifest=lambda: events.append("manifest.close"),
            compress=lambda: events.append("archive.compress"),
        )
        manager = ShutdownManager(
            runtime,
            thread_factory=HangingWorker,
            compress_timeout=0.01,
            batch_sender=lambda _root: events.append("json.send"),
        )

        manager.shutdown()

        # Зависшая отправка не блокирует освобождение ресурсов.
        self.assertNotIn("archive.compress", events)
        self.assertIn("cameras.release", events)
        self.assertIn("serial.close", events)

    def test_shutdown_default_batch_sender_is_production_scan(self):
        manager = ShutdownManager(
            RuntimeState(monitor=FakeMonitor([])),
            thread_factory=PassiveThread,
        )
        self.assertIs(manager.batch_sender, scan_and_send_all_batches)

    def test_after_window_closed_hides_console_before_cycle_stop(self):
        events = []
        monitor = FakeMonitor(events)
        runtime = RuntimeState(monitor=monitor)
        runtime.cycle = FakeCycle(events)
        manager = ShutdownManager(
            runtime,
            thread_factory=ImmediateThread,
            console_hider=lambda: events.append("console.hide"),
        )

        manager.after_window_closed()

        # Консоль скрывается сразу -- до ожидания остановки цикла, --
        # чтобы «чёрное окно» не висело до конца фоновой очистки.
        self.assertEqual(events[0], "console.hide")
        self.assertLess(
            events.index("console.hide"), events.index("cycle.force_exit")
        )

    def test_console_hide_failure_does_not_block_shutdown(self):
        events = []
        monitor = FakeMonitor(events)
        runtime = RuntimeState(monitor=monitor)
        runtime.cycle = FakeCycle(events)
        runtime.cameras = SimpleNamespace(
            release=lambda: events.append("cameras.release")
        )
        runtime.transport = FakeTransport(events)

        def failing_hider():
            raise RuntimeError("no console")

        manager = ShutdownManager(
            runtime,
            thread_factory=ImmediateThread,
            console_hider=failing_hider,
        )

        manager.after_window_closed()
        manager.shutdown()

        self.assertIn("cycle.force_exit", events)
        self.assertIn("cameras.release", events)
        self.assertIn("serial.close", events)


class DesktopUITest(unittest.TestCase):
    def test_webview_uses_monitor_configuration(self):
        events = []
        window = object()

        class Webview:
            @staticmethod
            def create_window(**kwargs):
                events.append(("create", kwargs))
                return window

            @staticmethod
            def start():
                events.append("start")

        monitor = SimpleNamespace(
            window_name="HMI",
            host="127.0.0.1",
            port=8123,
            fullscreen=True,
            webview_api=object(),
            _webview_window=None,
        )
        ui = DesktopUI(monitor, webview_module=Webview)

        ui.run()

        self.assertIs(monitor._webview_window, window)
        self.assertEqual(events[0][0], "create")
        self.assertEqual(events[0][1]["title"], "HMI")
        self.assertEqual(events[0][1]["url"], "http://127.0.0.1:8123/")
        self.assertTrue(events[0][1]["fullscreen"])
        self.assertIs(events[0][1]["js_api"], monitor.webview_api)
        self.assertEqual(events[1], "start")


class ApplicationRunnerTest(unittest.TestCase):
    def test_normal_ui_close_runs_post_window_stop_then_shutdown(self):
        events = []
        monitor = FakeMonitor(events)
        runtime = RuntimeState(monitor=monitor)
        initializer = SimpleNamespace(run=lambda: events.append("init"))
        exits = SimpleNamespace(
            bind=lambda: events.append("exit.bind"),
            request_exit=lambda: None,
        )

        class UI:
            def install_signal_handler(self, callback):
                events.append("signal")

            def print_startup_help(self):
                events.append("help")

            def run(self):
                events.append("ui.run")

        shutdown = SimpleNamespace(
            after_window_closed=lambda: events.append("after.ui"),
            shutdown=lambda: events.append("shutdown"),
        )
        app = ProductionApplication(
            runtime,
            initializer,
            exits,
            UI(),
            shutdown,
            thread_factory=PassiveThread,
        )

        app.run()

        self.assertLess(events.index("after.ui"), events.index("shutdown"))
        self.assertEqual(events[-1], "shutdown")

    def test_ui_error_still_runs_shutdown(self):
        events = []
        monitor = FakeMonitor(events)
        runtime = RuntimeState(monitor=monitor)
        initializer = SimpleNamespace(run=lambda: events.append("init"))
        exits = SimpleNamespace(
            bind=lambda: events.append("exit.bind"),
            request_exit=lambda: None,
        )

        class UI:
            def install_signal_handler(self, callback):
                events.append("signal")

            def print_startup_help(self):
                events.append("help")

            def run(self):
                events.append("ui.run")
                raise RuntimeError("ui failed")

        shutdown = SimpleNamespace(
            after_window_closed=lambda: events.append("after.ui"),
            shutdown=lambda: events.append("shutdown"),
        )
        app = ProductionApplication(
            runtime,
            initializer,
            exits,
            UI(),
            shutdown,
            thread_factory=PassiveThread,
        )

        with self.assertRaisesRegex(RuntimeError, "ui failed"):
            app.run()

        self.assertEqual(events[-1], "shutdown")
        self.assertNotIn("after.ui", events)

    def test_server_start_error_still_runs_shutdown(self):
        events = []
        monitor = FakeMonitor(events)
        monitor.server = FailingServer(events)
        runtime = RuntimeState(monitor=monitor)
        shutdown = ShutdownManager(runtime, thread_factory=ImmediateThread)
        app = ProductionApplication(
            runtime,
            SimpleNamespace(),
            SimpleNamespace(),
            SimpleNamespace(),
            shutdown,
            thread_factory=PassiveThread,
        )

        with self.assertRaisesRegex(RuntimeError, "server failed to start"):
            app.run()

        self.assertEqual(events[0], ("server.start", "127.0.0.1", 8000))
        self.assertIn("server.stop", events)

    def test_thread_start_error_still_stops_started_server(self):
        events = []
        monitor = FakeMonitor(events)
        runtime = RuntimeState(monitor=monitor)
        exits = SimpleNamespace(
            bind=lambda: events.append("exit.bind"),
            request_exit=lambda: None,
        )
        ui = SimpleNamespace()
        shutdown = ShutdownManager(runtime, thread_factory=ImmediateThread)
        app = ProductionApplication(
            runtime,
            SimpleNamespace(run=lambda: None),
            exits,
            ui,
            shutdown,
            thread_factory=FailingStartThread,
        )

        with self.assertRaisesRegex(RuntimeError, "thread failed to start"):
            app.run()

        self.assertEqual(events[0], ("server.start", "127.0.0.1", 8000))
        self.assertIn("server.stop", events)


if __name__ == "__main__":
    unittest.main()
