#!/usr/bin/env python3
"""
PhysiCar Audio Node - Multi-channel Audio Engine

Topic:
  - /audio: Audio streaming (PCM or encoded formats)

Channel Control:
  - Same channel = replace (stops existing playback)
  - Different channel = mix (simultaneous playback)
  - Stop via: {channel: "name", stop: true}
  - Stop all via: {stop_all: true}
  - Volume via: {channel: "name", volume: 0.5}
"""

import rclpy
from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy, HistoryPolicy
from physicar_interfaces.msg import Audio
import subprocess
import threading
import struct
import tempfile
import os
from queue import Queue, Empty


class AudioChannel:
    """Manages a single audio channel with internal queue."""
    
    def __init__(self, channel_name: str, logger):
        self.name = channel_name
        self.logger = logger
        self.process = None
        self.lock = threading.Lock()
        self.sample_rate = 16000
        self.channels = 1
        self.bits = 16
        self.volume = 1.0
        self.stop_flag = threading.Event()
        
        # Internal queue for PCM data
        self.pcm_queue = Queue()
        
        # Playback thread
        self.running = True
        self._start_playback_thread()
    
    def _start_playback_thread(self):
        """Start background thread to consume PCM queue."""
        def playback_loop():
            while self.running:
                try:
                    item = self.pcm_queue.get(timeout=0.1)
                    if item is None:
                        continue
                    
                    if self.stop_flag.is_set():
                        continue
                    
                    data, sample_rate, channels, bits = item
                    self._write_pcm(data, sample_rate, channels, bits)
                    
                except Empty:
                    continue
        
        self.playback_thread = threading.Thread(target=playback_loop, daemon=True)
        self.playback_thread.start()
    
    def enqueue(self, data: bytes, sample_rate: int, channels: int, bits: int):
        """Add PCM data to queue (non-blocking)."""
        if not self.stop_flag.is_set():
            self.pcm_queue.put((data, sample_rate, channels, bits))
    
    def stop(self):
        """Stop playback and clear queue."""
        self.stop_flag.set()
        
        # Clear queue
        while not self.pcm_queue.empty():
            try:
                self.pcm_queue.get_nowait()
            except Empty:
                break
        
        # Kill aplay
        with self.lock:
            if self.process and self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                self.process = None
        
        self.logger.info(f'[{self.name}] Stopped')
        return True
    
    def reset_stop_flag(self):
        """Reset stop flag for new playback."""
        self.stop_flag.clear()
    
    def set_volume(self, volume: float):
        """Set channel volume (0.0 ~ 1.0)."""
        self.volume = max(0.0, min(1.0, volume))
    
    def _write_pcm(self, data: bytes, sample_rate: int, channels: int, bits: int):
        """Actually write PCM data to aplay."""
        if self.stop_flag.is_set():
            return False
        
        if self.volume < 1.0 and bits == 16:
            data = self._apply_volume(data, self.volume)
        
        with self.lock:
            if (self.process is None or 
                self.process.poll() is not None or
                sample_rate != self.sample_rate or
                channels != self.channels or
                bits != self.bits):
                
                if self.process and self.process.poll() is None:
                    self.process.terminate()
                
                self.sample_rate = sample_rate
                self.channels = channels
                self.bits = bits
                
                fmt = f'S{bits}_LE'
                cmd = ['aplay', '-f', fmt, '-r', str(sample_rate),
                       '-c', str(channels), '-t', 'raw', '-q', '-']
                self.process = subprocess.Popen(cmd, stdin=subprocess.PIPE)
            
            try:
                self.process.stdin.write(data)
                self.process.stdin.flush()
                return True
            except BrokenPipeError:
                self.process = None
                return False
    
    def _apply_volume(self, data: bytes, volume: float) -> bytes:
        """Apply volume scaling to 16-bit PCM data."""
        samples = struct.unpack(f'<{len(data)//2}h', data)
        scaled = [int(s * volume) for s in samples]
        scaled = [max(-32768, min(32767, s)) for s in scaled]
        return struct.pack(f'<{len(scaled)}h', *scaled)


class AudioNode(Node):
    """Multi-channel audio engine node."""
    
    MAX_CHANNELS = 16
    CHUNK_SIZE = 4096
    
    def __init__(self):
        super().__init__('audio_node')
        
        self.channels = {}
        self.channels_lock = threading.Lock()
        self.playback_threads = {}
        
        audio_qos = QoSProfile(
            reliability=ReliabilityPolicy.RELIABLE,
            history=HistoryPolicy.KEEP_ALL,
            depth=10000
        )
        
        self.subscription = self.create_subscription(
            Audio, '/audio', self.audio_callback, audio_qos
        )
        
        self.get_logger().info('Audio engine started')
    
    def get_channel(self, channel_name: str) -> AudioChannel:
        """Get or create channel by name."""
        if not channel_name:
            channel_name = 'default'
        
        with self.channels_lock:
            if channel_name not in self.channels:
                if len(self.channels) >= self.MAX_CHANNELS:
                    channel_name = 'default'
                if channel_name not in self.channels:
                    self.channels[channel_name] = AudioChannel(channel_name, self.get_logger())
            return self.channels[channel_name]
    
    def audio_callback(self, msg: Audio):
        """Handle audio messages - stop is immediate, PCM goes to queue."""
        
        if msg.stop_all:
            self.stop_all()
            return
        
        channel = self.get_channel(msg.channel)
        
        # stop - immediate (clears queue)
        if msg.stop:
            channel.stop()
            return
        
        if msg.volume > 0:
            channel.set_volume(msg.volume)
        
        if not msg.data:
            return
        
        fmt = msg.format.lower() if msg.format else ""
        
        if fmt == "" or fmt == "pcm":
            if channel.stop_flag.is_set():
                channel.reset_stop_flag()
            
            channel.enqueue(
                bytes(msg.data),
                msg.sample_rate or 16000,
                msg.audio_channels or 1,
                msg.bits_per_sample or 16
            )
        else:
            self._play_encoded(channel, bytes(msg.data), fmt)
    
    def _play_encoded(self, channel: AudioChannel, data: bytes, fmt: str):
        """Decode and play encoded audio."""
        channel.stop()
        channel.reset_stop_flag()
        
        def stream_decoded():
            try:
                with tempfile.NamedTemporaryFile(suffix=f'.{fmt}', delete=False) as f:
                    f.write(data)
                    temp_path = f.name
                
                ffmpeg_cmd = [
                    'ffmpeg', '-i', temp_path, '-f', 's16le',
                    '-acodec', 'pcm_s16le', '-ar', '44100', '-ac', '2', '-'
                ]
                proc = subprocess.Popen(ffmpeg_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                
                while not channel.stop_flag.is_set():
                    chunk = proc.stdout.read(self.CHUNK_SIZE)
                    if not chunk:
                        break
                    channel.enqueue(chunk, 44100, 2, 16)
                
                proc.terminate()
                os.unlink(temp_path)
                
            except Exception as e:
                self.get_logger().error(f'[{channel.name}] Decode error: {e}')
        
        thread = threading.Thread(target=stream_decoded, daemon=True)
        thread.start()
        self.playback_threads[channel.name] = thread
    
    def stop_all(self):
        """Stop all channels."""
        with self.channels_lock:
            for channel in self.channels.values():
                channel.stop()
        self.get_logger().info('All channels stopped')


def main(args=None):
    rclpy.init(args=args)
    node = AudioNode()
    
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.stop_all()
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
