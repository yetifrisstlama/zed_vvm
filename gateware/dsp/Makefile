include ../../../bedrock/dir_list.mk
include $(BUILD_DIR)/top_rules.mk
include $(CORDIC_DIR)/rules.mk

vpath %.v $(CORDIC_DIR) $(DSP_DIR)

TARGET  = vvm_dsp
# TARGET  = reg_delay
# TARGET = phase_processor

SRC_V  += cordicg_b22.v cstageg.v addsubg.v
SRC_V  += mixer.v
SRC_V  += multi_sampler.v cic_multichannel.v
SRC_V  += serializer_multichannel.v reg_delay.v ccfilt.v double_inte_smp.v
SRC_V  += doublediff.v serialize.v grab_channels.v

all: $(TARGET).vcd

$(TARGET).v:
	(cd .. && python3 -m dsp.$(TARGET) build && mv $(TARGET).v dsp)

vvm_dsp_tb: $(SRC_V)

$(TARGET)_tb: $(TARGET).v

plot: $(TARGET).vcd
	python3 quickPlot.py output.txt --skip 100 --fs 117.6e6

CLEAN += cordicg_b22.v output.txt dds.v vvm_dsp.v $(TARGET)_tb $(TARGET).vcd \
		 $(TARGET).vcd.fst

include $(BUILD_DIR)/bottom_rules.mk
