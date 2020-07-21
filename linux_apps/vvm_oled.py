#!/usr/bin/python3
'''
display phase / magnitude on the OLED screen
'''
import logging
import sys
import signal
import time
from numpy import log10
from os import putenv, environ
from socket import gethostname
from random import randint, choice, random
import pygame as pg
from pygame.draw import line
from evdev import InputDevice
import argparse
import paho.mqtt.client as mqtt
from datetime import datetime

log = logging.getLogger('vvm_oled')


def set_led(isOn=True):
    ''' set the front-panel status LED '''
    with open('/sys/class/leds/led_status/brightness', 'w') as f:
        f.write('255' if isOn else '0')


class VvmOled:
    def __init__(self, args):
        self.pvs = {
            'mags': [-99] * 4,
            'phases': [0] * 3,
            'f_ref': 0,
            'f_ref_bb': 0,
            'f_tune': 0,
            'nyquist_band': 0,
            'vvm_pulse_wait_pre': 0,
            'vvm_pulse_wait_acq': 0,
            'vvm_pulse_wait_post': 1,
            'vvm_pulse_channel': 0
        }
        self.trig_ts = datetime.now()
        self.args = args

        self.mq = mqtt.Client('vvm_oled', True)
        self.mq.enable_logger(log)
        self.mq.on_connect = self.on_connect
        self.mq.connect_async(args.mqtt_server, args.mqtt_port, 60)
        self.mq.loop_start()

        self.mq.message_callback_add('vvm/results/#', self.on_result)
        self.mq.message_callback_add('vvm/settings/#', self.on_result)

        if not args.test:
            putenv('SDL_NOMOUSE', '')
            putenv('SDL_FBDEV', '/dev/fb1')
            putenv('SDL_FBACCEL', '0')
            putenv('SDL_VIDEODRIVER', 'fbcon')
        pg.display.init()
        pg.font.init()
        pg.mouse.set_visible(False)
        self.d = pg.display.set_mode((256, 64))  # returns the display surface

        fnts = {
            's': ['UbuntuMono-Regular', 16],
            'sbold': ['UbuntuMono-Bold', 16],
            'lbold': ['UbuntuMono-Bold', 24],
            'e': ['NotoEmoji-Regular', 16]
        }
        self.fnts = {}
        for k, v in fnts.items():
            self.fnts[k] = pg.font.Font('misc/fonts/{}.ttf'.format(v[0]), v[1])

        if not args.test:
            self.dev_rot = InputDevice('/dev/input/event0')
            self.dev_push = InputDevice('/dev/input/event1')

    def on_connect(self, client, userdata, flags, rc):
        log.info('MQTT connected %s %s', flags, rc)
        client.subscribe('vvm/#')

    def on_result(self, client, user, m):
        ''' convert mqtt payload to float and shove it into self.pvs '''
        try:
            if m.topic == 'vvm/results/trig_count':
                self.trig_ts = datetime.now()

            k = m.topic.split('/')[-1]
            old_val = self.pvs[k]
            t = type(old_val)
            if t is list:
                for i, val in enumerate(m.payload.split(b',')):
                    old_val[i] = float(val)
            else:
                self.pvs[k] = float(m.payload)
        except Exception as e:
            log.exception(e)

    def write(self, x, y, s, f='s', bold=False, white=False):
        '''
        write text to OLED surface with a bit of formating

        x, y is the upper left corner,
        s is a string to write
        f is the font key

        returns the lower right corner
        '''
        c = (0xA0, ) * 3
        if bold:
            f += 'bold'
        if white:
            c = (0xFF, ) * 3
        fo = self.fnts[f]
        sur = fo.render(s, True, c, (0, 0, 0))
        self.d.blit(sur, (x, y))
        return x + sur.get_width(), y + sur.get_height()

    def loop_forever(self):
        p = self.pvs
        while True:
            is_untune = abs(p['f_tune'] - p['f_ref_bb']) > 30e3
            is_low_power = p['mags'][0] < -30

            # ----------------------
            #  Draw on OLED
            # ----------------------
            self.d.fill((0x00, ) * 3)

            # Reference frequency
            x, y = 1, -2
            x, _ = self.write(x, y, 'REF: ')
            x, _ = self.write(
                x, y,
                '{:8.4f} MHz  '.format(p['f_ref'] / 1e6),
                bold=is_untune, white=is_untune
            )

            # Reference power
            x, _ = self.write(
                x, y,
                '{:5.1f} dBm'.format(p['mags'][0]),
                bold=is_low_power, white=is_low_power
            )

            # Check if trigger is timed out
            trig_dt = (datetime.now() - self.trig_ts).total_seconds()
            max_dt = p['vvm_pulse_wait_pre'] + p['vvm_pulse_wait_acq']
            max_dt = (max_dt + p['vvm_pulse_wait_post']) * 3
            is_timed_out = trig_dt > max_dt and p['vvm_pulse_channel'] <= 3

            # Warning symbol
            if is_untune or is_low_power or is_timed_out:
                self.write(233, -6, '⚠', 'e', False, True)

            # 3 x phase
            x, y = 0, 19
            if is_timed_out:
                x, _ = self.write(70, y, 'No trigger', 'l', True, True)
            else:
                for i, val in enumerate(p['phases']):
                    if p['mags'][i + 1] <= -60:
                        s = '{:>6s}'.format(' .... ')
                    else:
                        s = '{:>6.1f}°'.format(val)
                    x, _ = self.write(x, y, s, 'l', True, True)

            # 3 x power
            x, y = 4, 46
            for m in p['mags'][1:]:
                if is_timed_out:
                    x, _ = self.write(x, y, '  ... dBm  ')
                else:
                    x, _ = self.write(x, y, '{:>5.1f} dBm  '.format(m))

            # horizontal lines
            line(self.d, (0x10,) * 3, (0, 16), (255, 16), 2)
            line(self.d, (0x10,) * 3, (0, 44), (255, 44), 2)

            pg.display.update()

            # -----------------------
            #  Handle user input
            # -----------------------
            if not self.args.test:
                self.handle_input()
                set_led(not(is_untune or is_low_power))
            else:
                # In test mode, put some fake values in the variables instead
                p['mags'] = [randint(-600, 150) / 10 for i in range(4)]
                p['phases'] = [randint(-1800, 1800) / 10 for i in range(3)]
                p['f_ref_bb'] = randint(0, 117.6e6 / 2)
                p['f_tune'] = p['f_ref_bb'] + randint(-4000, 4000)
                p['f_ref'] = p['f_ref_bb'] + 117.6e6 * 4

            for event in pg.event.get():
                log.debug(str(event))
                if event.type == pg.QUIT:
                    pg.quit()

            # Delay locked to the wall clock for more accurate cycle time
            dt = 1 / self.args.fps
            time.sleep(dt - time.time() % dt)

    def handle_input(self):
        ''' returns encoder steps and button pushes '''
        rot = 0
        btn = False

        while True:
            # unfortunately using read() and try / except:
            # causes a huge memory leak
            evt = self.dev_rot.read_one()
            if evt is None:
                break
            if evt.type == 2:
                rot += evt.value

        while True:
            evt = self.dev_push.read_one()
            if evt is None:
                break
            btn |= evt.value

        n = self.pvs['nyquist_band'] + rot
        n = 0 if n < 0 else 13 if n > 13 else n
        if n != self.pvs['nyquist_band']:
            self.mq.publish('vvm/settings/nyquist_band', n, 0, True)

        if btn:
            # Triggers a single auto-tune
            self.mq.publish('vvm/settings/f_tune_set', 'auto')

        return rot, btn


def main():
    # systemd sends a SIGHUP at startup :p ignore it
    signal.signal(signal.SIGHUP, lambda x, y: print('SIGHUP'))

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        '--mqtt_server', default='localhost',
        help='Hostname / IP of the mqtt broker to connect to'
    )
    parser.add_argument(
        '--mqtt_port', default=1883,
        help='Port of the mqtt broker'
    )
    parser.add_argument(
        '--fps', default=30.0, type=float,
        help='Default frames per second'
    )
    parser.add_argument(
        '-v', '--verbose', action='store_true',
        help='Increase output verbosity'
    )
    parser.add_argument(
        '--test', action='store_true',
        help='Test mode. Open pygame window, show random numbers.'
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)

    app = VvmOled(args)
    app.loop_forever()


if __name__ == '__main__':
    main()
