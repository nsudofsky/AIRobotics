'''
Shared SimpleLE wrapper for all ME193 examples.

Usage:
    from lelib import singleMotor, doubleMotor, colorSensor, controller
'''

import time
import legoeducation as le


class _CardReader:
    """Mixin that adds card-tap reading to any LEGO Education device."""
    _last_card_serial = None

    def card_serial(self):
        """Return the serial number of the card currently on the sensor (0 = no card)."""
        return self.scanned_card.serial

    def card_tapped(self):
        """Return serial number when a new card is placed, else None.
        Tracks state internally so consecutive calls only fire once per tap."""
        current = self.scanned_card.serial
        if current != self._last_card_serial:
            self._last_card_serial = current
            if current != 0:
                return current
        return None


class singleMotor(_CardReader, le.SingleMotor):
    def __init__(self):
        super().__init__()

    def connect(self, card_serial, card_color=None):
        for attempt in range(5):
            try:
                super().connect(card_color=card_color, card_serial=card_serial)
                break
            except Exception as e:
                if "not ready" in str(e).lower() and attempt < 4:
                    time.sleep(1)
                else:
                    raise
        if not self.connected:
            raise ConnectionError('Error connecting to Single Motor.')

    def spin(self, rotations=1):
        self.motor_run_for_degrees(rotations * 360)

    def stop(self):
        self.motor_stop()

    def set_speed(self, speed):
        self.motor_set_speed(speed)

    def run(self, speed=50):
        # motor_run()'s speed accepts a signed value directly -- confirmed on
        # hardware that a negative speed simply reverses whichever direction
        # is passed (left at its CLOCKWISE default here).
        self.motor_run(speed=speed)


class doubleMotor(_CardReader, le.DoubleMotor):

    def connect(self, card_serial=None, card_color=None):
        """Connect to a Double Motor. With no arguments, connects to the
        first advertising Double Motor found (no Connection Card needed)."""
        for attempt in range(5):
            try:
                super().connect(card_color=card_color, card_serial=card_serial)
                break
            except Exception as e:
                if "not ready" in str(e).lower() and attempt < 4:
                    time.sleep(1)
                else:
                    raise
        if not self.connected:
            raise ConnectionError('Error connecting to Double Motor.')

    def move_steps(self, step=1):
        self.movement_move_for_degrees(-180 * step)

    def run(self, speed=50):
        # movement_move() takes the same signed-speed shortcut as motor_run()
        # (see singleMotor.run() above); this specific method wasn't
        # hardware-tested itself, but it's built the same way internally.
        self.movement_move(speed=speed)

    def run_time(self, time=2000):
        self.movement_move_for_time(time)

    def run_left(self, degrees=None):
        if degrees is None:
            self.motor_run(direction=le.MOTOR_MOVE_DIRECTION_COUNTERCLOCKWISE, motor=le.MOTOR_LEFT)
        else:
            self.motor_run_for_degrees(degrees=degrees, direction=le.MOTOR_MOVE_DIRECTION_COUNTERCLOCKWISE, motor=le.MOTOR_LEFT)

    def run_right(self, degrees=None):
        if degrees is None:
            self.motor_run(direction=le.MOTOR_MOVE_DIRECTION_COUNTERCLOCKWISE, motor=le.MOTOR_RIGHT)
        else:
            self.motor_run_for_degrees(degrees=degrees, direction=le.MOTOR_MOVE_DIRECTION_COUNTERCLOCKWISE, motor=le.MOTOR_RIGHT)

    def turn_left(self, degrees=90):
        self.movement_turn_for_degrees(degrees, direction=le.MOVEMENT_TURN_DIRECTION_LEFT)

    def turn_right(self, degrees=90):
        self.movement_turn_for_degrees(degrees, direction=le.MOVEMENT_TURN_DIRECTION_RIGHT)

    def set_speed(self, speed):
        self.motor_set_speed(speed, motor=le.MOTOR_LEFT)
        self.motor_set_speed(speed, motor=le.MOTOR_RIGHT)
        self.movement_set_speed(speed)

    def set_speed_left(self, speed):
        self.motor_set_speed(speed, motor=le.MOTOR_LEFT)

    def set_speed_right(self, speed):
        self.motor_set_speed(speed, motor=le.MOTOR_RIGHT)

    def stop(self):
        self.motor_stop()

    # ── IMU convenience methods ──────────────────────────────────────────────

    def reset_heading(self):
        """Zero the yaw at the current orientation (call once before driving)."""
        self.imu_reset_yaw_axis(0)

    def yaw(self):
        """Yaw in degrees since the last reset_heading() call.
        Positive = clockwise (drifted right), negative = counter-clockwise."""
        return float(self.imu_device.yaw)

    def gyro_z(self):
        """Z-axis angular velocity (raw int16). Positive = rotating clockwise."""
        return float(self.imu_device.gyroscopeZ)


class controller(_CardReader, le.Controller):

    def connect(self, card_serial, card_color=None):
        for attempt in range(5):
            try:
                super().connect(card_color=card_color, card_serial=card_serial)
                break
            except Exception as e:
                if "not ready" in str(e).lower() and attempt < 4:
                    time.sleep(1)
                else:
                    raise
        if not self.connected:
            raise ConnectionError('Error connecting to Controller.')

    def left_up(self):        return self.sensor.leftPercent > 0
    def left_down(self):      return self.sensor.leftPercent < 0
    def left_released(self):  return self.sensor.leftPercent == 0
    def right_up(self):       return self.sensor.rightPercent > 0
    def right_down(self):     return self.sensor.rightPercent < 0
    def right_released(self): return self.sensor.rightPercent == 0
    def left_position(self):  return self.sensor.leftPercent
    def right_position(self): return self.sensor.rightPercent

    def drive(self, dm, t=100):
        for i in range(t):
            dm.movement_move_tank(self.left_position(), self.right_position())
            time.sleep(0.1)


class colorSensor(_CardReader, le.ColorSensor):
    def __init__(self):
        super().__init__()

    def connect(self, card_serial, card_color=None):
        for attempt in range(5):
            try:
                super().connect(card_color=card_color, card_serial=card_serial)
                break
            except Exception as e:
                if "not ready" in str(e).lower() and attempt < 4:
                    time.sleep(1)
                else:
                    raise
        if not self.connected:
            raise ConnectionError('Error connecting to Color Sensor.')

    def reflection(self):
        return self.sensor.reflection

    def detect_color(self):
        color_mapping = {
            0: 'No color', 1: 'Red',     2: 'Yellow', 3: 'Blue',
            4: 'Teal',     5: 'Green',   6: 'Purple', 7: 'White',
            8: 'Magenta',  9: 'Orange', 10: 'Azure'
        }
        return color_mapping.get(self.sensor.color, 'Unknown')

    def raw_rgb(self):
        """Raw red, green, blue as 16-bit integers (0–65535)."""
        return (self.sensor.rawRed, self.sensor.rawGreen, self.sensor.rawBlue)

    def raw_reading(self):
        """Full sensor snapshot for classification.
        Returns a dict with all channels normalized to 0–1."""
        MAX16 = 65535.0
        MAX8  =   255.0
        return {
            "rawRed":     self.sensor.rawRed     / MAX16,
            "rawGreen":   self.sensor.rawGreen   / MAX16,
            "rawBlue":    self.sensor.rawBlue    / MAX16,
            "reflection": self.sensor.reflection / MAX8,
            "hue":        self.sensor.hue        / MAX16,
            "saturation": self.sensor.saturation / MAX8,
            "value":      self.sensor.value      / MAX8,
        }
