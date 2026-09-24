import legoeducation as le


motor = le.SingleMotor()
motor.connect()

if not motor.connected:
    print("Could not connect to the LEGO motor.")
    raise SystemExit(1)

try:
    # Run one full rotation at 50% speed.
    motor.motor_run_for_degrees(360, speed=50)
finally:
    motor.disconnect()