"""Deterministic process tests with measured time, no HA or real equipment."""

import importlib.util
import unittest
from pathlib import Path

PATH = Path(__file__).parents[1] / "custom_components/home_control/environment.py"
SPEC = importlib.util.spec_from_file_location("environment_control", PATH)
ENV = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ENV)
HUM_SPEC = importlib.util.spec_from_file_location("humidity_control", PATH.with_name("humidity.py"))
HUM = importlib.util.module_from_spec(HUM_SPEC)
HUM_SPEC.loader.exec_module(HUM)
CONFIG = {
    "sample_interval": 60,
    "window_size": 5,
    "minimum_samples": 3,
    "rising_slope": 0.3,
    "falling_slope": -0.2,
    "stable_slope": 0.12,
    "stable_range": 0.8,
    "stable_windows_required": 3,
    "uncertain_timeout_minutes": 20,
    "plateau_duration": 300,
}


class MotionTests(unittest.TestCase):
    def test_any_on_and_only_all_off_starts_timer(self):
        c = ENV.MotionLighting({"light_off_delay": 300})
        self.assertTrue(c.evaluate(0, ["on", "unavailable"], "off"))
        self.assertIsNone(c.evaluate(1, ["off", "unavailable"], "on"))
        self.assertIsNone(c.evaluate(500, ["off", "off"], "on"))
        self.assertIsNone(c.evaluate(799, ["off", "off"], "on"))
        self.assertFalse(c.evaluate(800, ["off", "off"], "on"))

    def test_motion_and_missing_sensor_cancel_old_timer(self):
        c = ENV.MotionLighting({"light_off_delay": 300})
        c.evaluate(0, ["off"], "on")
        c.evaluate(299, ["on"], "on")
        self.assertIsNone(c.evaluate(300, ["off"], "on"))
        c.evaluate(599, [None], "on")
        self.assertIsNone(c.evaluate(600, ["off"], "on"))
        self.assertFalse(c.evaluate(900, ["off"], "on"))

    def test_manual_on_without_motion_gets_full_delay(self):
        c = ENV.MotionLighting({"light_off_delay": 600})
        c.evaluate(0, ["off"], "off")
        self.assertIsNone(c.evaluate(100, ["off"], "on"))
        self.assertFalse(c.evaluate(700, ["off"], "on"))


class SharedFanTests(unittest.TestCase):
    def setUp(self):
        self.c = ENV.SharedVentilation({"fan_on_delay": 120, "fan_off_delay": 60})

    def test_other_room_changes_do_not_restart_on_delay(self):
        self.assertIsNone(self.c.evaluate(0, ["on", "off"], ["off"], "off"))
        self.assertIsNone(self.c.evaluate(100, ["on", "on"], ["off"], "off"))
        self.assertTrue(self.c.evaluate(120, ["off", "on"], ["off"], "off"))

    def test_immediate_bathroom_boost_and_delayed_release(self):
        self.assertTrue(self.c.evaluate(0, ["off", "off"], ["on"], "off"))
        self.assertTrue(self.c.evaluate(100, ["off", "off"], ["on"], "on"))
        self.assertIsNone(self.c.evaluate(101, ["off", "off"], ["off"], "on"))
        self.assertFalse(self.c.evaluate(161, ["off", "off"], ["off"], "on"))

    def test_new_use_cancels_off_and_preserves_running_fan(self):
        self.c.evaluate(0, ["off"], ["off"], "on")
        self.assertIsNone(self.c.evaluate(59, ["on"], ["off"], "on"))
        self.assertIsNone(self.c.evaluate(60, ["off"], ["off"], "on"))
        self.assertFalse(self.c.evaluate(120, ["off"], ["off"], "on"))

    def test_unknown_input_never_proves_safe_to_turn_off(self):
        self.c.evaluate(0, ["off"], ["off"], "on")
        self.assertIsNone(self.c.evaluate(61, ["off"], ["unavailable"], "on"))
        self.assertIsNone(self.c.evaluate(100, ["off"], ["off"], "on"))
        self.assertFalse(self.c.evaluate(160, ["off"], ["off"], "on"))


class HumidityTests(unittest.TestCase):
    def setUp(self):
        self.c = HUM.HumidityVentilation(CONFIG)

    def sample(self, at, value, fan="on", light="on", **kwargs):
        return self.c.evaluate(at, light, fan, value, **kwargs)

    def test_actual_elapsed_minutes_determine_slope(self):
        self.assertEqual(HUM.trend([(0, 40), (120, 40.4), (240, 40.8)], CONFIG)[0], "FLUCTUATING")
        self.assertAlmostEqual(HUM.trend([(0, 40), (60, 40.4), (120, 40.8)], CONFIG)[1], 0.4)

    def test_rising_requires_occupied_room_and_confirmed_fan_start(self):
        for i, value in enumerate([40, 41]):
            self.assertIsNone(self.sample(i * 60, value, fan="off"))
        self.assertTrue(self.sample(120, 42, fan="off"))
        self.assertEqual(self.c.phase, "STARTING")
        self.assertIsNone(self.c.fan_since)
        self.sample(121, 42, fan="on")
        self.assertEqual(self.c.phase, "VENTILATING")
        self.assertEqual(self.c.fan_since, 121)

    def test_no_start_in_empty_room_or_after_growth_has_finished(self):
        for i, value in enumerate([40, 41, 42]):
            self.assertIsNone(self.sample(i * 60, value, fan="off", light="off"))
        for i in range(3, 9):
            self.sample(i * 60, 42, fan="off", light="off")
        self.assertIsNone(self.sample(540, 42, fan="off", light="on"))
        self.assertEqual(self.c.phase, "IDLE")

    def test_falling_continues_beyond_twenty_minutes(self):
        for i in range(35):
            self.assertIsNone(self.sample(i * 60, 80 - i * 0.5, light="off"))
        self.assertEqual(self.c.trend, "FALLING")

    def test_rising_has_priority_over_time(self):
        for i in range(35):
            self.assertIsNone(self.sample(i * 60, 40 + i * 0.5))
        self.assertEqual(self.c.trend, "RISING")

    def test_plateau_requires_duration_and_fresh_samples_even_with_light_on(self):
        results = [self.sample(i * 60, 55) for i in range(8)]
        self.assertTrue(all(value is None for value in results[:-1]))
        self.assertFalse(results[-1])
        self.assertEqual(self.c.stop_reason, "humidity_plateau")

    def test_short_plateau_then_new_rise_does_not_stop(self):
        values = [60, 59, 58, 57, 56, 56, 56, 56, 56, 56, 57, 58, 59]
        for i, value in enumerate(values):
            self.assertIsNone(self.sample(i * 60, value))
        self.assertEqual(self.c.trend, "RISING")
        self.assertEqual(self.c.stable, 0)

    def test_fall_fluctuation_and_fall_after_twenty_minutes(self):
        values = [80 - i * 0.5 for i in range(30)] + [65.5, 65.8, 65.6, 65.9, 65.7, 65, 64, 63]
        for i, value in enumerate(values):
            self.assertIsNone(self.sample(i * 60, value))
        self.assertEqual(self.c.trend, "FALLING")

    def test_sensor_loss_watchdog_runs_without_measurements(self):
        self.assertIsNone(self.sample(0, None))
        self.assertIsNone(self.sample(1199, None))
        self.assertFalse(self.sample(1200, None))
        self.assertEqual(self.c.stop_reason, "uncertain_trend_timeout")

    def test_sensor_loss_after_drying_gets_its_own_grace(self):
        for i in range(30):
            self.sample(i * 60, 80 - i * 0.5)
        self.assertIsNone(self.sample(1800, None))
        self.assertIsNone(self.sample(2999, None))
        self.assertFalse(self.sample(3000, None))

    def test_invalid_values_and_stale_data_are_not_plateau(self):
        for value in ("unavailable", float("nan"), float("inf"), -1, 101):
            with self.subTest(value=value):
                self.c = HUM.HumidityVentilation(CONFIG)
                self.sample(0, value)
                self.assertFalse(self.c.sensor_valid)
                self.assertEqual(len(self.c.history), 0)
        self.sample(60, 50, fresh=False)
        self.assertFalse(self.c.sensor_valid)

    def test_repeated_cached_report_is_not_a_new_sample(self):
        for i in range(10):
            self.assertIsNone(self.sample(i * 60, 50, reported_at=0))
        self.assertEqual(len(self.c.history), 1)

    def test_manual_off_does_not_immediately_restart(self):
        for i in range(5):
            self.sample(i * 60, 40 + i)
        self.assertIsNone(self.sample(300, 45, fan="off"))
        self.assertTrue(self.c.inhibited)
        self.assertIsNone(self.sample(360, 46, fan="off"))

    def test_plateau_stop_rearms_only_after_off_then_new_growth(self):
        for i in range(8):
            result = self.sample(i * 60, 50)
        self.assertFalse(result)
        self.sample(480, 50, fan="off")
        self.assertFalse(self.c.inhibited)
        for i, value in enumerate([50, 51, 52], start=9):
            result = self.sample(i * 60, value, fan="off")
        self.assertTrue(result)

    def test_recovery_clears_stale_history(self):
        for i in range(3):
            self.sample(i * 60, 50)
        self.sample(121, None)
        self.sample(180, 55)
        self.assertEqual(self.c.trend, "WARMUP")
        self.assertEqual(len(self.c.history), 1)

    def test_manual_restart_adopts_a_new_run_after_external_off(self):
        self.sample(0, 50)
        self.sample(60, 51, fan="off")
        self.assertTrue(self.c.inhibited)
        self.sample(61, 51, fan="on")
        self.assertFalse(self.c.inhibited)
        self.assertEqual(self.c.phase, "ADOPTED")
        self.assertEqual(self.c.fan_since, 61)

    def test_short_stable_period_does_not_reset_long_unproductive_fluctuations(self):
        c = HUM.HumidityVentilation(
            CONFIG | {"window_size": 3, "rising_slope": 2, "falling_slope": -2, "stable_range": 0.1}
        )
        # Noise below rising/falling thresholds, with occasional brief plateaus.
        for i in range(20):
            value = 50 if i % 5 in (0, 1, 2) else 51
            self.assertIsNone(c.evaluate(i * 60, "on", "on", value))
        result = c.evaluate(1200, "on", "on", 50)
        if result is None:
            result = c.evaluate(1260, "on", "on", 51)
        self.assertFalse(result)
        self.assertEqual(c.stop_reason, "uncertain_trend_timeout")
