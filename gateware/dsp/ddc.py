'''
4 channel digital down converter for the vector voltmeter
TODO block diagram !

try:
    python3 ddc.py build
'''
from sys import argv

from migen import *
from litex.soc.interconnect.csr import AutoCSR, CSRStorage
from migen.genlib.cdc import MultiReg
from .dds import DDS


class VVM_DDC(Module, AutoCSR):
    def __init__(self, adcs=None, DAVR=4, OSCW=18, OUT_W=21, DI_W=37, PCW=13):
        '''
        adcs:  List of ADC input Signals of width DW
        DAVR:  Mixer guard bits
        OSCW:  Width of the Local Oscillator DDS output
        OUT_W: IQ stream output width (aka. cc_outw)
        DI_W:  Double Integrator width, increase for higher decimation factors
        PCW:   cic_period counter width (maximum decimation factor)
        '''
        # ---------------------------
        #  ADC inputs
        # ---------------------------
        if adcs is None:
            # Mock input for simulation
            adcs = [Signal((14, True)) for i in range(4)]
        self.adcs = adcs

        # ---------------------------
        #  Some more fixed parameters
        # ---------------------------
        DW = len(adcs[0])  # input sample width
        # width of each I, Q channel at the mixer output
        MIX_W = DW + DAVR

        # ---------------------------
        # host-settable parameters
        # ---------------------------
        # in the sample clock domain
        self.cic_period = Signal(PCW)  # expected values 33 to 33*128
        self.cic_shift = Signal(4)  # expected values 7 to 15

        # ---------------------------
        #  IQ stream output
        # ---------------------------
        # <strobe_cc high> I0, Q0, I1, Q1, I2, Q2, I3, Q3 <strobe_cc low>
        # self.source = source = stream.Endpoint(("iq_data", OUT_W))

        self.result_iq = Signal((OUT_W, True))
        self.result_strobe = Signal()  # high when valid samples are output
        self.result_first = Signal()  # pulse when first sample is output

        ###

        # [I0, Q0, I1, Q1, ...]
        mixouts = []

        # Connect each input channel to an I and Q mixer with separate LOs
        self.submodules.dds = DDS(len(adcs), OSCW)
        for i, adc in enumerate(adcs):
            for dds_o in (self.dds.o_coss[i], self.dds.o_sins[i]):
                mix_o = Signal((MIX_W, True))
                self.specials += Instance(
                    'mixer',
                    p_NORMALIZE=False,
                    p_dwi=DW,
                    p_davr=DAVR,
                    p_dwlo=OSCW,
                    i_clk=ClockSignal(),
                    i_adcf=adc,
                    i_mult=dds_o,
                    o_mixout=mix_o
                )
                mixouts.append(mix_o)

        # Times the 'taking one out of N samples' part
        cic_sample = Signal()
        self.specials += Instance(
            'multi_sampler',
            p_sample_period_wi=PCW,
            i_clk=ClockSignal(),
            i_ext_trig=1,
            i_sample_period=self.cic_period,
            i_dsample0_period=Constant(1, 8),
            i_dsample1_period=Constant(1, 8),
            i_dsample2_period=Constant(1, 8),
            o_sample_out=cic_sample
        )

        # Poly-phase CIC down-conversion, output is a sample stream
        self.specials += Instance(
            'cic_multichannel',
            # Okay, this is really strange and I don't understand it.
            # Without using Constant() here I get I get 4'd8 in ddc.v,
            # which breaks the reg_delay module!!!!!!!
            # It doesn't output the MSBs of `shifter` as it should.
            # However, putting a $display() in reg_delay and printing
            # its parameters, there is no difference visible at all.
            # Also I was not able to re-produce this error in a simpler
            # setup (reg_delay_tb.v)
            # very strange things are happening inside iverilog!!!!
            # p_n_chan=len(adcs) * 2,
            p_n_chan=Constant(len(adcs) * 2, 32),
            p_di_dwi=MIX_W,
            p_di_rwi=DI_W,
            p_cc_outw=OUT_W,
            # NOTE: Setting to 1 to compensate for removed /2 from double_inte
            p_di_noise_bits=1,
            p_cc_halfband=False,
            p_cc_use_delay=False,
            # Bits to discard after CIC (added to i_cic_shift)
            p_cc_shift_base=7,

            i_clk=ClockSignal(),
            i_reset=ResetSignal(),
            i_stb_in=1,
            i_d_in=Cat(mixouts),
            i_cic_sample=cic_sample,
            i_cc_sample=1,
            i_cc_shift=self.cic_shift,

            o_cc_sr_out=self.result_iq,
            o_cc_stb_out=self.result_strobe
        )

        result_strobe_ = Signal()
        self.sync += result_strobe_.eq(self.result_strobe)
        self.comb += self.result_first.eq(self.result_strobe & ~result_strobe_)

    def add_csr(self):
        self.ddc_deci = CSRStorage(len(self.cic_period), reset=48, name="deci")
        self.ddc_shift = CSRStorage(len(self.cic_shift), reset=0, name="shift")
        self.specials += [
            MultiReg(self.ddc_deci.storage, self.cic_period, 'sample'),
            MultiReg(self.ddc_shift.storage, self.cic_shift, 'sample')
        ]
        self.dds.add_csr()


def main():
    tName = argv[0].replace('.py', '')
    d = ClockDomainsRenamer('sample')(VVM_DDC())
    if 'build' in argv:
        ''' generate a .v file for simulation with Icarus / general usage '''
        from migen.fhdl.verilog import convert
        convert(
            d,
            ios={
                *d.adcs,
                d.cic_period,
                d.cic_shift,
                d.result_iq,
                d.result_strobe
            },
            display_run=True
        ).write(tName + '.v')


if __name__ == '__main__':
    if len(argv) <= 1:
        print(__doc__)
        exit(-1)
    main()
