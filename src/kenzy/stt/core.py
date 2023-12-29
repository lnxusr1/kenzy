from ctypes import CFUNCTYPE, cdll, c_char_p, c_int
import logging
import queue
import collections
import webrtcvad
import pyaudio
import sys
import traceback
import wave
import io
import soundfile
import threading
from kenzy.extras import py_error_handler


def speech_model(model_name="openai/whisper-tiny.en"):
    from transformers import AutoProcessor, AutoModelForSpeechSeq2Seq
    
    processor = AutoProcessor.from_pretrained(model_name)
    model = AutoModelForSpeechSeq2Seq.from_pretrained(model_name)
    model.config.forced_decoder_ids = None

    return processor, model


def read_from_device(stop_event, muted_event=threading.Event(), **kwargs):

    stop_event.clear()
    muted_event.clear()
    
    audio_device_index = kwargs.get("audio.device")
    audio_channels = kwargs.get("audio.channels", 1)
    audio_sample_rate = kwargs.get("audio.sample_rate", 16000)
    vad_aggressiveness = kwargs.get("speech.vad_aggressiveness", 0)
    speech_buffer_padding = kwargs.get("speech.buffer_padding", 350)
    speech_buffer_size = kwargs.get("speech.buffer_size", 50)
    speech_ratio = kwargs.get("speech.ratio", 0.75)

    processor, model = speech_model(kwargs.get("speech.model", "openai/whisper-tiny.en"))

    buffer_queue = queue.Queue()

    def proxy_callback(in_data, frame_count, time_info, status):
        buffer_queue.put(in_data)
        return (None, pyaudio.paContinue)

    ring_buffer = collections.deque(
        maxlen=speech_buffer_padding // (1000 * int(audio_sample_rate / float(speech_buffer_size)) // audio_sample_rate))

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
        logging.getLogger("AUD-READ").debug(str(sys.exc_info()[0]))
        logging.getLogger("AUD-READ").debug(str(traceback.format_exc()))
        logging.getLogger("AUD-READ").error("Unable to read from listener device.")
        return

    triggered = False
    container = io.BytesIO()
    wf = wave.open(container, "wb")
    wf.setnchannels(audio_channels)
    wf.setsampwidth(_audio_device.get_sample_size(pyaudio.paInt16))
    wf.setframerate(audio_sample_rate)

    while not stop_event.is_set():
        frame = buffer_queue.get()

        if len(frame) >= 640:
            if muted_event.is_set():
                continue

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

                    if not stop_event.is_set():
                        if text.strip() != "":

                            yield text[:255] if len(text) > 255 else text

                        container = io.BytesIO()
                        wf = wave.open(container, "wb")
                        wf.setnchannels(audio_channels)
                        wf.setsampwidth(_audio_device.get_sample_size(pyaudio.paInt16))
                        wf.setframerate(audio_sample_rate)
