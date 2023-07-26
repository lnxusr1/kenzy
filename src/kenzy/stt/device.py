import threading
import logging
import time
import queue
import collections
import webrtcvad
import pyaudio
import sys
import traceback
import wave
import io
import soundfile
from ctypes import CFUNCTYPE, cdll, c_char_p, c_int
from kenzy.core import KenzySuccessResponse, KenzyErrorResponse
from kenzy.stt.core import speech_model, py_error_handler


class AudioProcessor:
    type = "kenzy.stt"

    location = None
    group = None
    service = None

    logger = logging.getLogger("KNZY-STT")
    settings = {}
 
    stop_event = threading.Event()
    main_thread = None
    callback_thread = None
    callback_queue = None

    def __init__(self, **kwargs):
        self.settings = kwargs

        self.location = kwargs.get("location")
        self.group = kwargs.get("group")

    @property
    def accepts(self):
        return ["start", "stop", "restart", "status", "get_settings", "set_settings", "collect"]

    def _process_callback(self):
        while True:
            data = self.callback_queue.get()
            if data is None or not isinstance(data, str):
                break

            print(data)
            self.service.collect(data={
                "type": "kenzy.stt",
                "data": data
            })

    def _read_from_device(self):

        self.stop_event.clear()

        audio_device_index = self.settings.get("audio_device")
        audio_channels = self.settings.get("audio_channels", 1)
        audio_sample_rate = self.settings.get("audio_sample_rate", 16000)
        vad_aggressiveness = self.settings.get("vad_aggressiveness", 0)
        speech_buffer_padding = self.settings.get("speech_buffer_padding", 350)
        speech_buffer_size = self.settings.get("speech_buffer_size", 50)
        speech_ratio = self.settings.get("speech_ratio", 0.75)

        processor, model = speech_model(self.settings.get("speech_model", "openai/whisper-tiny.en"))

        buffer_queue = queue.Queue()
        self._isRunning = True

        def proxy_callback(in_data, frame_count, time_info, status):
            buffer_queue.put(in_data)
            return (None, pyaudio.paContinue)

        ring_buffer = collections.deque(
            maxlen=speech_buffer_padding //
            (1000 * int(audio_sample_rate /
                        float(speech_buffer_size)) // audio_sample_rate))

        _vad = webrtcvad.Vad(vad_aggressiveness)

        ERROR_HANDLER_FUNC = CFUNCTYPE(None, c_char_p, c_int, c_char_p, c_int, c_char_p)
        c_error_handler = ERROR_HANDLER_FUNC(py_error_handler)
        asound = cdll.LoadLibrary('libasound.so')
        asound.snd_lib_error_set_handler(c_error_handler)

        _audio_device = pyaudio.PyAudio()

        try:
            stream = _audio_device.open(
                format=pyaudio.paInt16,
                channels=int(audio_channels) if audio_channels is not None else 1,
                rate=audio_sample_rate,
                input=True,
                frames_per_buffer=int(audio_sample_rate / float(speech_buffer_size)),
                input_device_index=audio_device_index,
                stream_callback=proxy_callback
            )

            stream.start_stream()
        except Exception:
            logging.debug(str(sys.exc_info()[0]))
            logging.debug(str(traceback.format_exc()))
            self.logger.error("Unable to read from listener device.")
            self.stop()
            return False

        triggered = False
        container = io.BytesIO()
        wf = wave.open(container, "wb")
        wf.setnchannels(audio_channels)
        wf.setsampwidth(_audio_device.get_sample_size(pyaudio.paInt16))
        wf.setframerate(audio_sample_rate)

        while not self.stop_event.is_set():
            frame = buffer_queue.get()

            if len(frame) >= 640:  # and not self._isAudioOut:
                is_speech = _vad.is_speech(frame, audio_sample_rate)
            
                if not triggered:
                    ring_buffer.append((frame, is_speech))
                    num_voiced = len([f for f, speech in ring_buffer if speech])

                    if num_voiced > speech_ratio * ring_buffer.maxlen:
                        triggered = True

                        for f in ring_buffer:
                            wf.writeframes(f[0])

                        ring_buffer.clear()
                else:
                    wf.writeframes(frame)
                    ring_buffer.append((frame, is_speech))
                    num_unvoiced = len([f for f, speech in ring_buffer if not speech])
                    if num_unvoiced > speech_ratio * ring_buffer.maxlen:
                        triggered = False

                        container.seek(0)
                        data, _ = soundfile.read(container)

                        input_features = processor(
                            data,
                            sampling_rate=audio_sample_rate,
                            return_tensors="pt"
                        ).input_features  # Batch size 1
                        generated_ids = model.generate(input_features=input_features)

                        text = processor.batch_decode(generated_ids, skip_special_tokens=True)
                        text = text[0]
                        if text.startswith("</s>"):
                            text = text[4:]
                        if text.endswith("</s>"):
                            text = text[:-4]
                        text = text.strip()

                        wf.close()

                        if not self.stop_event.is_set():
                            if text.strip() != "":

                                self.logger.info("HEARD " + text)
                                self.callback_queue.put(text)

                            container = io.BytesIO()
                            wf = wave.open(container, "wb")
                            wf.setnchannels(audio_channels)
                            wf.setsampwidth(_audio_device.get_sample_size(pyaudio.paInt16))
                            wf.setframerate(audio_sample_rate)

    def collect(self, data=None, context=None):
        self.logger.debug(f"{data}, {self.type}, {context.get()}")

    def is_alive(self, **kwargs):
        if self.main_thread is not None and self.main_thread.is_alive():
            return True
        
        return False
    
    def start(self, **kwargs):

        if self.is_alive():
            self.logger.error("Audio Processor already running")
            return KenzyErrorResponse("Audio Processor already running")

        self.callback_queue = queue.Queue()
        self.callback_thread = threading.Thread(target=self._process_callback, daemon=True)
        self.callback_thread.start()

        self.main_thread = threading.Thread(target=self._read_from_device, daemon=True)
        self.main_thread.start()

        if self.is_alive():
            self.logger.info("Started Video Processor")
            return KenzySuccessResponse("Started Video Processor")
        else:
            self.logger.error("Unable to start Video Processor")
            return KenzyErrorResponse("Unable to start Video Processor")
        
    def stop(self, **kwargs):
        if self.main_thread is None or not self.main_thread.is_alive():
            self.logger.error("Video Processor is not running")
            return KenzyErrorResponse("Video Processor is not running")
        
        self.stop_event.set()
        self.main_thread.join()

        self.callback_queue.put(None)
        self.callback_thread.join()

        if not self.is_alive():
            self.logger.info("Stopped Video Processor")
            return KenzySuccessResponse("Stopped Video Processor")
        else:
            self.logger.error("Unable to stop Video Processor")
            return KenzyErrorResponse("Unable to stop Video Processor")
    
    def restart(self, **kwargs):
        if self.read_thread is not None or self.proc_thread is not None:
            ret = self.stop()
            if not ret.is_success():
                return ret
        
        return self.start()

    def set_service(self, service):
        self.service = service

    def set_settings(self, **kwargs):
        return KenzyErrorResponse("Not implemented")
    
    def status(self, **kwargs):
        return KenzySuccessResponse({
            "active": self.is_alive(),
            "type": self.type,
            "accepts": self.accepts,
            "data": {
            }
        })
