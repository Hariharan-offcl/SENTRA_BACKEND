import time
import math
import lgpio

import rclpy
from rclpy.node import Node
from sensor_msgs.msg import LaserScan


FRONT_TRIG = 24
FRONT_ECHO = 25

REAR_TRIG = 5
REAR_ECHO = 6

MAX_DIST = 2.0


class UltrasonicScan(Node):
    def __init__(self):
        super().__init__('ultrasonic_scan')

        self.h = lgpio.gpiochip_open(4)

        lgpio.gpio_claim_output(self.h, FRONT_TRIG, 0)
        lgpio.gpio_claim_input(self.h, FRONT_ECHO)

        lgpio.gpio_claim_output(self.h, REAR_TRIG, 0)
        lgpio.gpio_claim_input(self.h, REAR_ECHO)

        self.pub = self.create_publisher(LaserScan, '/scan', 10)

        self.timer = self.create_timer(0.1, self.publish_scan)

        self.get_logger().info('SENTRA ultrasonic scan ready')

    def distance(self, trig, echo):
        lgpio.gpio_write(self.h, trig, 1)
        time.sleep(0.00001)
        lgpio.gpio_write(self.h, trig, 0)

        start = time.monotonic()

        while not lgpio.gpio_read(self.h, echo):
            if time.monotonic() - start > 0.03:
                return MAX_DIST

        pulse_start = time.monotonic()

        while lgpio.gpio_read(self.h, echo):
            if time.monotonic() - pulse_start > 0.03:
                return MAX_DIST

        pulse_end = time.monotonic()

        distance = (pulse_end - pulse_start) * 343.0 / 2.0

        return min(distance, MAX_DIST)

    def publish_scan(self):
        front = self.distance(FRONT_TRIG, FRONT_ECHO)
        rear = self.distance(REAR_TRIG, REAR_ECHO)

        scan = LaserScan()

        scan.header.stamp = self.get_clock().now().to_msg()
        scan.header.frame_id = 'laser'

        scan.angle_min = -math.pi
        scan.angle_max = math.pi / 2
        scan.angle_increment = math.pi / 2

        scan.range_min = 0.02
        scan.range_max = MAX_DIST

        scan.ranges = [
            rear,
            MAX_DIST,
            front,
            MAX_DIST,
        ]

        self.pub.publish(scan)

        self.get_logger().info(
            f'Front: {front:.2f} m | Rear: {rear:.2f} m'
        )

    def destroy_node(self):
        lgpio.gpio_write(self.h, FRONT_TRIG, 0)
        lgpio.gpio_write(self.h, REAR_TRIG, 0)
        lgpio.gpiochip_close(self.h)
        super().destroy_node()


def main():
    rclpy.init()
    node = UltrasonicScan()

    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()


if __name__ == '__main__':
    main()