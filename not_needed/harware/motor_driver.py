import rclpy
from rclpy.node import Node
from geometry_msgs.msg import Twist
import lgpio

ENA, IN1, IN2 = 12, 17, 27
ENB, IN3, IN4 = 13, 22, 23

class MotorDriver(Node):
    def __init__(self):
        super().__init__('motor_driver')

        self.h = lgpio.gpiochip_open(4)

        for pin in [ENA, IN1, IN2, ENB, IN3, IN4]:
            lgpio.gpio_claim_output(self.h, pin, 0)

        self.sub = self.create_subscription(
            Twist, '/cmd_vel', self.cmd_vel, 10
        )

        self.get_logger().info('SENTRA motor driver ready')

    def motor(self, en, a, b, speed):
        if speed > 0:
            lgpio.gpio_write(self.h, a, 1)
            lgpio.gpio_write(self.h, b, 0)
        elif speed < 0:
            lgpio.gpio_write(self.h, a, 0)
            lgpio.gpio_write(self.h, b, 1)
        else:
            lgpio.gpio_write(self.h, a, 0)
            lgpio.gpio_write(self.h, b, 0)

        lgpio.tx_pwm(self.h, en, 1000, min(abs(speed), 100))

    def cmd_vel(self, msg):
        linear = msg.linear.x
        angular = msg.angular.z

        left = linear - angular
        right = linear + angular

        left = max(-1.0, min(1.0, left))
        right = max(-1.0, min(1.0, right))

        self.motor(ENA, IN1, IN2, left * 100)
        self.motor(ENB, IN3, IN4, right * 100)

    def destroy_node(self):
        self.motor(ENA, IN1, IN2, 0)
        self.motor(ENB, IN3, IN4, 0)
        lgpio.gpiochip_close(self.h)
        super().destroy_node()


def main():
    rclpy.init()
    node = MotorDriver()

    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()

        if rclpy.ok():
            rclpy.shutdown()


if __name__ == '__main__':
    main()