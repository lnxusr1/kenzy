import os
import io
import wave
import queue
import soundfile
import webrtcvad
import collections
import threading
from kenzy.stt.core import speech_model

import numpy as np
from openwakeword.model import Model
import logging

class Consumer:
    @property
    def is_running(self):
        return False
    
    def initialize(self, *args, **kwargs):
        return True
    
    def put(self, *args, **kwargs):
        return True

    def start(self, *args, **kwargs):
        return True

    def stop(self, *args, **kwargs):
        return True
    
class SpeechModel(Consumer):
    def __init__(self, *args, **kwargs):

        self.thread = None

        self._is_running = False
        self.buffer_queue = queue.Queue()
        
        self.audio_channels = kwargs.get("audio.channels", 1)
        self.audio_sample_rate = kwargs.get("audio.sample_rate", 16000)
        self.audio_sample_size = kwargs.get("audio.sample_size", 2)
        self.vad_aggressiveness = kwargs.get("speech.vad_aggressiveness", 0)
        self.speech_buffer_padding = kwargs.get("speech.buffer_padding", 350)
        self.speech_buffer_size = kwargs.get("speech.buffer_size", 50)
        self.speech_ratio = kwargs.get("speech.ratio", 0.75)
        self.frame_length = kwargs.get("audio.frame_length", 640)
        self.device_type = kwargs.get("speech.model_target", "cpu")
        self.compute_type = kwargs.get("speech.compute_type", "int8")

        if self.device_type == "gpu":
            self.device_type = "cuda"

        self.wwm = Model(wakeword_models=[os.path.join(os.path.dirname(__file__), "..", "data", "ken_zee.tflite")])

    def initialize(self, stop_event=threading.Event(), muted_event=threading.Event(), callback=None, *args, **kwargs):        

        self.stop_event=stop_event
        self.muted_event=muted_event

        self.vad = webrtcvad.Vad(self.vad_aggressiveness)

        model = speech_model(
            kwargs.get("speech.model", "tiny"), 
            offline=kwargs.get("offline", False), 
            compute_type=self.compute_type, 
            device_type=self.device_type
        )

        #self.processor = processor
        self.model = model

        self.callback = callback

    @property
    def is_running(self):
        return self._is_running if self._is_running else self.thread.is_alive() if self.thread is not None else False

    def put(self, data, *args, **kwargs):
        self.buffer_queue.put(data)
        return True

    def _get_wave(self):
        container = io.BytesIO()
        wf = wave.open(container, "wb")
        wf.setnchannels(self.audio_channels)
        wf.setsampwidth(self.audio_sample_size)  # for pyaudio.paInt16
        wf.setframerate(self.audio_sample_rate)

        return container, wf

    def _run(self, *args, **kwargs):
        
        ring_buffer = collections.deque(
            maxlen = self.speech_buffer_padding // (1000 * int(self.audio_sample_rate / float(self.speech_buffer_size)) // self.audio_sample_rate))

        triggered = False
        wakeword = False
        container, wf = self._get_wave()

        self._is_running = True

        while not self.stop_event.is_set():
            frame = self.buffer_queue.get()

            if len(frame) >= self.frame_length:
                if self.muted_event.is_set():
                    continue

                if not wakeword and not triggered:
                    np_audio = np.frombuffer(frame, dtype=np.int16)
                    self.wwm.predict(np_audio)
                    for mdl in self.wwm.prediction_buffer.keys():
                        scores = list(self.wwm.prediction_buffer[mdl])
                        if scores[-1] > 0.5:
                            logging.critical("Wakeword Detected!")
                            wakeword = True

                if wakeword:
                    is_speech = self.vad.is_speech(frame, self.audio_sample_rate)
                
                    if not triggered:
                        ring_buffer.append((frame, is_speech))
                        num_voiced = len([f for f, speech in ring_buffer if speech])

                        if num_voiced > self.speech_ratio * ring_buffer.maxlen:
                            triggered = True

                            for f in ring_buffer:
                                wf.writeframes(f[0])

                            ring_buffer.clear()
                    else:
                        wf.writeframes(frame)
                        ring_buffer.append((frame, is_speech))
                        num_unvoiced = len([f for f, speech in ring_buffer if not speech])
                        if num_unvoiced > self.speech_ratio * ring_buffer.maxlen:
                            triggered = False
                            wakeword = False

                            container.seek(0)
                            data, _ = soundfile.read(container)


                            text = ""
                            segments, info = self.model.transcribe(data, beam_size=1, language="en")  # or language="en", etc.
                            for segment in segments:
                                #print(f"{segment.start:.2f}s --> {segment.end:.2f}s: {segment.text}")
                                text = segment.text
                            
                            #text = text[0]
                            if text.startswith("</s>"):
                                text = text[4:]
                            if text.endswith("</s>"):
                                text = text[:-4]
                            text = text.strip()

                            wf.close()

                            if not self.stop_event.is_set():
                                if str(text).strip() != "":

                                    if self.callback is not None:
                                        self.callback(text[:255] if len(text) > 255 else text)

                                container, wf = self._get_wave()

        self._is_running = False

    def start(self, *args, **kwargs):
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()


class Streamer(Consumer):
    def __init__(self, *args, **kwargs):
        pass