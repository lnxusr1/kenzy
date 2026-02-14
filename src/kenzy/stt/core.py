from ctypes import CFUNCTYPE, cdll, c_char_p, c_int
import logging
import os
import pyaudio
import sys
import traceback
import threading
from kenzy.extras import py_error_handler
from faster_whisper import WhisperModel


def speech_model(model_size="tiny", offline=True, compute_type="int8", device_type="cpu"):
    offline_base = os.path.expanduser("~/.kenzy/cache/models")
    os.makedirs(offline_base, exist_ok=True)

    model = WhisperModel(
        model_size,
        device=device_type, # "cuda" if torch.cuda.is_available() else "cpu",
        compute_type=compute_type,
        download_root=offline_base
    )

    return model  # this replaces both processor and model


def read_from_device(stop_event, muted_event=threading.Event(), audio_consumers=[], **kwargs):

    stop_event.clear()
    muted_event.clear()
    
    audio_device_index = kwargs.get("audio.device")
    audio_channels = kwargs.get("audio.channels", 1)
    audio_sample_rate = kwargs.get("audio.sample_rate", 16000)
    speech_buffer_size = kwargs.get("speech.buffer_size", 50)

    def proxy_callback(in_data, frame_count, time_info, status):
        for ac in audio_consumers:
            ac.put(in_data)

        return (None, pyaudio.paContinue)

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
        logging.getLogger("AUD-READ").debug(str(sys.exc_info()[0]))
        logging.getLogger("AUD-READ").debug(str(traceback.format_exc()))
        logging.getLogger("AUD-READ").error("Unable to read from listener device.")
        return None

    return stream
