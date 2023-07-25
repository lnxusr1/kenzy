import threading
import multiprocessing
import queue
import pyaudio
import collections
import webrtcvad
from ctypes import CFUNCTYPE, cdll, c_char_p, c_int
import logging
import sys
import traceback
import io
import wave
import soundfile
import torch
# from transformers import Speech2TextProcessor, Speech2TextForConditionalGeneration
# from transformers import WhisperProcessor, WhisperForConditionalGeneration
from transformers import AutoProcessor, AutoModelForSpeechSeq2Seq

def py_error_handler(filename, line, function, err, fmt):
    """
    Error handler to translate non-critical errors to logging messages.
    
    Args:
        filename (str): Output file name or device (/dev/null).
        line (int): Line number of error
        function (str): Function containing
        err (Exception): Exception raised for error
        fmt (str): Format of log output
    """

    # Convert the parameters to strings for logging calls
    fmt = fmt.decode("utf-8")
    filename = filename.decode("utf-8")
    fnc = function.decode('utf-8')
    
    # Setting up a logger so you can turn these errors off if so desired.
    logger = logging.getLogger("CTYPES")

    if (fmt.count("%s") == 1 and fmt.count("%i") == 1):
        logger.debug(fmt % (fnc, line))
    elif (fmt.count("%s") == 1):
        logger.debug(fmt % (fnc))
    elif (fmt.count("%s") == 2):
        logger.debug(fmt % (fnc, str(err)))
    else:
        logger.debug(fmt)
    return


def threaded(fn):
    """
    Thread wrapper shortcut using @threaded prefix

    Args:
        fn (function):  The function to executed on a new thread.

    Returns:
        (thread):  New thread for executing function.
    """

    def wrapper(*args, **kwargs):
        thread = threading.Thread(target=fn, args=args, kwargs=kwargs)
        thread.daemon = True
        thread.start()
        return thread

    return wrapper


def pthreaded(fn):
    """
    Process wrapper shortcut using @pthreaded prefix

    Args:
        fn (function):  The function to executed on a new process.

    Returns:
        (process):  New process for executing function.
    """

    def wrapper(*args, **kwargs):
        pthread = multiprocessing.Process(target=fn, args=args, kwargs=kwargs)
        pthread.daemon = True
        pthread.start()
        return pthread

    return wrapper


class Listener:
    def __init__(self):
        self.logger = logging.getLogger("LISTENER")
        self.args = {
            "audioDeviceIndex": None,
            "audioChannels": 1,
            "audioSampleRate": 16000,
            "vadAggressiveness": 0,
            "speechRatio": 0.75,
            "speechBufferSize": 50,
            "speechBufferPadding": 350,
            "speechModel": "openai/whisper-tiny.en"  
        }

        # openai/whisper-large-v2
        # facebook/s2t-large-librispeech-asr

        self._callbackHandler = None
        self.process = None
        self._isRunning = False

    def _doCallback(self, inData):
        try:
            if self._callbackHandler is not None:
                self._callbackHandler("AUDIO_INPUT", inData)
            else:
                print({"type": "AUDIO_INPUT", "data": inData})
        except Exception:
            pass

        return

    # @pthreaded
    def _readFromMic(self, args):

        audioDeviceIndex = args.get("audioDeviceIndex", 0)
        audioChannels = args.get("audioChannels", 1)
        audioSampleRate = args.get("audioSampleRate", 16000)
        vadAggressiveness = args.get("vadAggressiveness", 0)
        speechBufferPadding = args.get("speechBufferPadding", 350)
        speechBufferSize = args.get("speechBufferSize", 50)
        speechRatio = args.get("speechRatio", 0.75)

        # model = Speech2TextForConditionalGeneration.from_pretrained(args.get("speechModel"))
        # processor = Speech2TextProcessor.from_pretrained(args.get("speechModel"))

        # processor = WhisperProcessor.from_pretrained("openai/whisper-large-v2")
        # model = WhisperForConditionalGeneration.from_pretrained("openai/whisper-large-v2")

        #processor = WhisperProcessor.from_pretrained("openai/whisper-tiny")
        #model = WhisperForConditionalGeneration.from_pretrained("openai/whisper-tiny")

        #processor = WhisperProcessor.from_pretrained("openai/whisper-tiny.en")
        #model = WhisperForConditionalGeneration.from_pretrained("openai/whisper-tiny.en")

        # processor = WhisperProcessor.from_pretrained("openai/whisper-medium")
        # model = WhisperForConditionalGeneration.from_pretrained("openai/whisper-medium")

        processor = AutoProcessor.from_pretrained(args.get("speechModel"))
        model = AutoModelForSpeechSeq2Seq.from_pretrained(args.get("speechModel"))
        model.config.forced_decoder_ids = None

        buffer_queue = queue.Queue()
        self._isRunning = True

        def proxy_callback(in_data, frame_count, time_info, status):
            buffer_queue.put(in_data)
            return (None, pyaudio.paContinue)

        ring_buffer = collections.deque(
            maxlen=speechBufferPadding //
            (1000 * int(audioSampleRate /
                        float(speechBufferSize)) // audioSampleRate))

        _vad = webrtcvad.Vad(vadAggressiveness)

        ERROR_HANDLER_FUNC = CFUNCTYPE(None, c_char_p, c_int, c_char_p, c_int, c_char_p)
        c_error_handler = ERROR_HANDLER_FUNC(py_error_handler)
        asound = cdll.LoadLibrary('libasound.so')
        asound.snd_lib_error_set_handler(c_error_handler)

        _audio_device = pyaudio.PyAudio()

        try:
            stream = _audio_device.open(
                format=pyaudio.paInt16,
                channels=int(audioChannels) if audioChannels is not None else 1,
                rate=audioSampleRate,
                input=True,
                frames_per_buffer=int(audioSampleRate / float(speechBufferSize)),
                input_device_index=audioDeviceIndex,
                stream_callback=proxy_callback
            )

            stream.start_stream()
        except Exception:
            logging.debug(str(sys.exc_info()[0]))
            logging.debug(str(traceback.format_exc()))
            self.logger.error("Unable to read from listener device.")
            self._isRunning = False
            self.stop()
            return False

        self._isRunning = True
        triggered = False
        container = io.BytesIO()
        wf = wave.open(container, "wb")
        wf.setnchannels(audioChannels)
        wf.setsampwidth(_audio_device.get_sample_size(pyaudio.paInt16))
        wf.setframerate(audioSampleRate)

        while self._isRunning:
            frame = buffer_queue.get()

            if len(frame) >= 640:  # and not self._isAudioOut:
                is_speech = _vad.is_speech(frame, audioSampleRate)
            
                if not triggered:
                    ring_buffer.append((frame, is_speech))
                    num_voiced = len([f for f, speech in ring_buffer if speech])

                    if num_voiced > speechRatio * ring_buffer.maxlen:
                        triggered = True

                        for f in ring_buffer:
                            wf.writeframes(f[0])

                        ring_buffer.clear()
                else:
                    wf.writeframes(frame)
                    ring_buffer.append((frame, is_speech))
                    num_unvoiced = len([f for f, speech in ring_buffer if not speech])
                    if num_unvoiced > speechRatio * ring_buffer.maxlen:
                        triggered = False

                        container.seek(0)
                        data, rate = soundfile.read(container)

                        input_features = processor(
                            data,
                            sampling_rate=audioSampleRate,
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

                        if self._isRunning:
                            if text.strip() != "":

                                self.logger.info("HEARD " + text)
                                self._doCallback(text)

                            container = io.BytesIO()
                            wf = wave.open(container, "wb")
                            wf.setnchannels(audioChannels)
                            wf.setsampwidth(_audio_device.get_sample_size(pyaudio.paInt16))
                            wf.setframerate(audioSampleRate)


    def start(self):
        self._readFromMic(self.args)

    def stop(self):
        pass


if __name__ == "__main__":
    logging.basicConfig(
        datefmt='%Y-%m-%d %H:%M:%S %z',
        format='%(asctime)s %(name)-12s - %(levelname)-9s - %(message)s',
        level=logging.DEBUG
    )

    d = Listener()
    d.start()