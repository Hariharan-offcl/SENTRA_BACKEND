"""Sitting/fall state machine regression checks."""
import os
import sys
import importlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from services import fall_detection as fd


def main():
    importlib.reload(fd)
    t = 100.0
    # Mid-frame, moderate aspect ratio remains seated through slow movement.
    fd.sample(1, [100, 100, 150, 180], .9, t, frame_height=600)
    seated = fd.sample(1, [100, 105, 150, 180], .9, t + .25, frame_height=600)
    assert seated["state"] == fd.SITTING, seated
    # Rapid descent with a wide bbox starts a possible fall.
    fd.sample(2, [100, 100, 120, 300], .9, t, frame_height=600)
    possible = fd.sample(2, [100, 300, 350, 150], .9, t + .25, frame_height=600)
    assert possible["state"] == fd.POSSIBLE_FALL, possible
    confirmed = fd.sample(2, [100, 300, 350, 150], .9,
                          t + .25 + fd.CONFIRM_TIME_S + .1, frame_height=600)
    assert confirmed["state"] == fd.FALL_CONFIRMED, confirmed
    # One transition fires hooks once, even when subsequent samples arrive.
    fd.reset()
    hooks = []
    fd.register_emergency_hook(hooks.append)
    fd.sample(3, [100, 100, 120, 300], .9, t, frame_height=600)
    fd.sample(3, [100, 300, 350, 150], .9, t + .25, frame_height=600)
    for i in (1, 2):
        fd.sample(3, [100, 300, 350, 150], .9,
                  t + .25 + fd.CONFIRM_TIME_S + i, frame_height=600)
    assert len(hooks) == 1, len(hooks)
    # Recovery before the timer returns to NORMAL; seated stand-up is also safe.
    fd.reset()
    fd.sample(4, [100, 100, 120, 300], .9, t, frame_height=600)
    fd.sample(4, [100, 300, 350, 150], .9, t + .25, frame_height=600)
    assert fd.sample(4, [100, 100, 120, 300], .9, t + .5,
                     frame_height=600)["state"] == fd.NORMAL
    fd.reset()
    fd.sample(5, [100, 100, 150, 180], .9, t, frame_height=600)
    assert fd.sample(5, [100, 100, 120, 300], .9, t + .25,
                     frame_height=600)["state"] == fd.NORMAL
    print("Phase 23 fall/sitting checks passed")


if __name__ == "__main__":
    main()
