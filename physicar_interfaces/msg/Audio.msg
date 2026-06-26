# Audio streaming message
# Multi-channel support for simultaneous playback
# Supports PCM raw data or encoded formats (mp3, wav, ogg, etc.)

# Channel name (e.g., "default", "bgm", "effect")
# Same channel = replace, different channel = mix
string channel "default"

# Audio data (PCM or encoded)
uint8[] data

# Data format: "" or "pcm" = raw PCM, "mp3", "wav", "ogg", etc.
string format ""

# PCM format (only used when format="" or "pcm")
uint32 sample_rate 16000    # e.g., 16000, 44100, 48000
uint8 audio_channels 1      # 1=mono, 2=stereo
uint8 bits_per_sample 16    # 8, 16, 24, 32

float32 volume 0.5          # 0.0 ~ 1.0

# Control
bool stop false             # true = stop playback on this channel
bool stop_all false         # true = stop all channels
