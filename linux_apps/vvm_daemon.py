#!/usr/bin/python3
'''
MQTT client to expose VVM measurements and controls

-------------
 MQTT topics
-------------
vvm/settings/<arg_name>
    Most of the command line arguments can be set over mqtt

vvm/settings/phase_reset
    Any pub resets the DDS phase-accumulators in the digital down-converter

vvm/settings/f_tune_set
    Set the digital down-converter center frequency to message value in [Hz]
    Write `auto` to tune on the frequency counter value (f_ref_bb)

vvm/results/mags 9.3,-8.7,-7.6,-74.3
    Magnitude values of the REF, A, B, C channels [dBm]

vvm/results/phases 92.1,-53.5,-24.5
    Phase values of the A, B, C channels against REF [deg]

vvm/results/f_ref_bb 7310921.52
    Raw frequency counter value (base-band) [Hz]

vvm/results/f_ref 124910921.52
    Frequency counter value taking selected Nyquist-band into account [Hz]

vvm/results/f_tune 7310928.576
    Center frequency of the digital down-converter (base-band) [Hz]

'''
import logging
import signal
import time
from numpy import array
from argparse import ArgumentParser, RawDescriptionHelpFormatter

from lib.mqtt_pvs import MqttPvs
from lib.csr_lib import CsrLib
from lib.vvm_helpers import initLTC, initSi570, meas_f_ref, \
    CalHelper, getRealFreq

log = logging.getLogger('vvm_daemon')


class VvmApp:
    def __init__(self, args, c):
        self.args = args
        self.c = c

        prefix = 'vvm/settings/'
        self.pvs = MqttPvs(args, prefix, {
            # DEFAULT, MIN, MAX, WRITE_TO_HW
            # DEFAULT = None: take it from args

            'fps':          [None, 0.01, 120],
            'nyquist_band': [None, 0, 13],
            'vvm_iir':      [None, 0, 13, True],
            'vvm_ddc_shift':[None, 1, 64, True],
            'vvm_ddc_deci': [None, 10, 500, True],

            # Measurement harmonics (measure at M time f_ref)
            'M_A':          [None, 1, 15, False],
            'M_B':          [None, 1, 15, False],
            'M_C':          [None, 1, 15, False],

            # Pulse trigger
            'vvm_pulse_channel':   [None, 0, 7, True],
            'vvm_pulse_threshold': [None, 0, (2**21 - 1), True],
            'vvm_pulse_wait_pre':  [None, 0, 10.0, lambda x: int(x * args.fs)],
            'vvm_pulse_wait_acq':  [None, 0, 10.0, lambda x: int(x * args.fs)],
            'vvm_pulse_wait_post': [None, 0, 10.0, lambda x: int(x * args.fs)]
        }, c)
        self.mq = self.pvs.mq

        # Trigger auto / manually tuning when publishing to settings/f_tune_set
        # the current tuning value can be read from results/f_tune
        self.mq.message_callback_add(
            prefix + 'f_tune_set', lambda c, d, m: self.tune(m.payload)
        )

        self.mq.message_callback_add(prefix + 'phase_reset', self.pr)

        # Print some CSRs for debugging
        log.info('ddc_ftw %s', hex(c.read_reg('vvm_ddc_dds_ftw0')))
        log.info('f_sample %s', args.fs)
        deci = c.read_reg('vvm_ddc_deci')
        log.info('ddc_deci %s', deci)
        log.info('ddc_shift %s', c.read_reg('vvm_ddc_shift'))
        log.info('BW %.3f MHz', args.fs / deci / 1e6)
        log.info('iir_shift %s', c.read_reg('vvm_iir'))

        # ----------------------------------------------
        #  Load calibration
        # ----------------------------------------------
        self.cal = CalHelper(args.cal_file, args.vvm_ddc_shift, c, args.fs)

    def pr(self, *args):
            ''' Reset DDS phase accumulators of down-converter '''
            self.c.write_reg('vvm_ddc_dds_ctrl', 0x01)

    def loop_forever(self):
        # Just came out of reset, give freq. counter some time to accumulate
        time.sleep(2.5)

        cycle = 0
        trig_count_ = 0
        last_ts = 0
        while True:
            ts = time.time()

            # do some housekeeping things every second
            if ts - last_ts > 1.0:
                last_ts = ts

                # Measure and publish f_ref frequency
                self.f_ref_bb = meas_f_ref(self.c, self.args.fs)
                f_ref = getRealFreq(
                    self.pvs.nyquist_band, self.f_ref_bb, self.args.fs
                )

                # Aliased frequency of REF input measured by frequency counter
                self.mq.publish('vvm/results/f_ref_bb', self.f_ref_bb)

                # Absolute frequency of REF input, needs user selected f-band
                self.mq.publish('vvm/results/f_ref', f_ref)

            if cycle == 0:
                self.tune(self.f_ref_bb)

                # Reset DDS phase accumulators once at startup after setting Ms
                self.pr()

            update_meas = False
            if self.pvs.vvm_pulse_channel > 3:
                # CW mode
                update_meas = True
            else:
                # pulsed trigger mode, wait for incremented trig_count
                trig_count = self.c.read_reg('vvm_pulse_trig_count')
                if trig_count > trig_count_:
                    update_meas = True
                    trig_count_ = trig_count
                    self.mq.publish('vvm/results/trig_count', str(trig_count))

            if update_meas:
                Ms = array([1, self.pvs.M_A, self.pvs.M_B, self.pvs.M_C])

                mags = self.cal.get_mags(
                    f_ref * Ms, int(self.pvs.vvm_ddc_shift)
                )
                phases = self.cal.get_phases(f_ref * Ms[1:])

                # Publish multiple values per topic (separated by ,)
                temp = ','.join([str(v) for v in mags])
                self.mq.publish('vvm/results/mags', temp)

                vals = [self.c.read_reg("vvm_mag" + str(i)) for i in range(4)]
                temp = ','.join([str(v) for v in vals])
                self.mq.publish('vvm/results/raw_mags', temp)

                temp = ','.join([str(v) for v in phases])
                self.mq.publish('vvm/results/phases', temp)

            # Delay locked to the wall clock for more accurate cycle time
            dt = 1 / self.pvs.fps
            time.sleep(dt - time.time() % dt)
            cycle += 1

    def tune(self, f_tune=None):
        '''
        set down-converter center frequency to f_tune
        if f_tune is None, use the frequency counter on the REF channel
        '''
        # Fall back to last measured frequency
        if f_tune is None or f_tune == b'auto':
            f_tune = self.f_ref_bb
        else:
            try:
                f_tune = float(f_tune)
            except (TypeError, ValueError):
                log.warning('cannot tune to %s, try `auto`', f_tune)
                return

        ftw = int((f_tune / self.args.fs) * 2**32)

        Ms = array([1, self.pvs.M_A, self.pvs.M_B, self.pvs.M_C])

        for i, m in enumerate(Ms):
            ftw_ = int(ftw * m)
            self.c.write_reg('vvm_ddc_dds_ftw' + str(i), ftw_)
            if i > 0:
                self.c.write_reg('vvm_pp_mult' + str(i), int(m))

        self.c.write_reg('vvm_ddc_dds_ctrl', 0x02)  # FTW update

        self.mq.publish('vvm/results/f_tune', f_tune, 0, True)
        log.info('tuned f_ref to {:6f} MHz'.format(f_tune / 1e6))


def main():
    # systemd sends a SIGHUP at startup :p ignore it
    signal.signal(signal.SIGHUP, lambda x, y: log.warning('SIGHUP ignored'))

    parser = ArgumentParser(
        description=__doc__, formatter_class=RawDescriptionHelpFormatter
    )
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
        help='Default measurements per second'
    )
    parser.add_argument(
        '--vvm_ddc_deci', default=100, type=int,
        help='Digital down-conversion decimation factor'
    )
    parser.add_argument(
        '--vvm_ddc_shift', default=2, type=int,
        help='Bits to discard after down conversion to prevent overflow'
    )
    parser.add_argument(
        '--vvm_iir', default=10, type=int,
        help='IIR filter for result averaging. Smoothing factor from 0 - 15.'
    )
    parser.add_argument(
        '--fs', default=117.6e6, type=float,
        help='ADC sample rate [MHz]. Must match hello_LTC.py setting.'
    )
    parser.add_argument(
        '--cal_file', default='cal2_att.npz',
        help='Amplitude / Phase calibration file'
    )
    parser.add_argument(
        '--nyquist_band', default=8, type=int,
        help='Initial nyquist band (N * fs / 2)'
    )
    parser.add_argument(
        '--M_A', default=1, type=int,
        help='f_ref multiplier for channel A'
    )
    parser.add_argument(
        '--M_B', default=1, type=int,
        help='f_ref multiplier for channel B'
    )
    parser.add_argument(
        '--M_C', default=1, type=int,
        help='f_ref multiplier for channel C'
    )
    parser.add_argument(
        '--vvm_pulse_channel', default=7, type=int,
        help='Channel to trigger on for pulsed measurements, CW mode for > 3'
    )
    parser.add_argument(
        '--vvm_pulse_threshold', default=1024, type=int,
        help='Power threshold for trigger [raw]'
    )
    parser.add_argument(
        '--vvm_pulse_wait_pre', default=2e-6, type=float,
        help='Delay after trigger before acquisition [s]'
    )
    parser.add_argument(
        '--vvm_pulse_wait_acq', default=20e-6, type=float,
        help='Width of the acquisition window [s]'
    )
    parser.add_argument(
        '--vvm_pulse_wait_post', default=1.0, type=float,
        help='Post acquisition hold-off time [s]'
    )
    parser.add_argument(
        '-v', '--verbose', action='store_true',
        help='increase output verbosity'
    )

    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)

    with CsrLib(0x40000000, 'csr.json') as c:
        log.info('FPGA ident: %s', c.get_ident())

        initSi570(c, args.fs)
        initLTC(c, check_align=True)

        # Sampling Frequency
        time.sleep(2)
        log.info('fs = {:6f} MHz, should be {:6f} MHz'.format(
            c.read_reg('lvds_f_sample_value') / 1e6, args.fs / 1e6
        ))

        app = VvmApp(args, c)
        app.loop_forever()


if __name__ == '__main__':
    main()
