from ctypes import CFUNCTYPE, cdll, c_char_p, c_int
import torch
import soundfile as sf
import hashlib
import pyaudio
import wave
import os
import subprocess
import sys
import traceback
from kenzy.extras import py_error_handler
import logging
import tempfile
import threading


def model_type(type="speecht5", target=None, offline=False):
    model = { "type": type }

    if str(type).lower().strip() == "speecht5":
        from transformers import SpeechT5Processor, SpeechT5ForTextToSpeech, SpeechT5HifiGan
        from datasets import load_dataset

        device = target
        if device is None:
            device = "cuda" if torch.cuda.is_available() else "cpu"

        logger = logging.getLogger("KNZY-TTS")
        logger.info(f"Using device={device} for speech generation.")
        
        offline_base = os.path.expanduser("~/.kenzy/cache/models")
        model_name = "microsoft/speecht5_tts"
        vocoder_name = "microsoft/speecht5_hifigan"

        if os.path.exists(offline_base):
            os.makedirs(os.path.expanduser("~/.kenzy/cache/models"), exist_ok=True)
            
        if not offline or not os.path.exists(os.path.join(offline_base, model_name)):
            processor = SpeechT5Processor.from_pretrained("microsoft/speecht5_tts")
            tts_model = SpeechT5ForTextToSpeech.from_pretrained("microsoft/speecht5_tts").to(device)
            
            processor.save_pretrained(os.path.join(offline_base, model_name))
            tts_model.save_pretrained(os.path.join(offline_base, model_name))
            
        else:
            processor = SpeechT5Processor.from_pretrained(os.path.join(offline_base, model_name), local_files_only=True)
            tts_model = SpeechT5ForTextToSpeech.from_pretrained(os.path.join(offline_base, model_name), local_files_only=True).to(device)
            
        if not offline or not os.path.exists(os.path.join(offline_base, vocoder_name)):
            vocoder = SpeechT5HifiGan.from_pretrained(vocoder_name).to(device)
            vocoder.save_pretrained(os.path.join(offline_base, vocoder_name))

        else:
            vocoder = SpeechT5HifiGan.from_pretrained(os.path.join(offline_base, vocoder_name), local_files_only=True).to(device)
        
        embeddings_dataset = load_dataset("Matthijs/cmu-arctic-xvectors", split="validation")
        
        speakers = {
            'awb': 0,     # Scottish male
            'bdl': 1138,  # US male
            'clb': 2271,  # US female
            'jmk': 3403,  # Canadian male
            'ksp': 4535,  # Indian male
            'rms': 5667,  # US male
            'slt': 6799   # US female
        }

        model = { 
            "type": type, 
            "device": device, 
            "processor": processor,
            "model": tts_model,
            "vocoder": vocoder,
            "dataset": embeddings_dataset,
            "speakers": speakers
        }

    return model


def create_speech(model, text, speaker="slt", cache_folder="~/.kenzy/cache/speech", ext_prg=None):

    if cache_folder is not None:
        os.makedirs(os.path.expanduser(cache_folder), exist_ok=True)

    if model.get("type") == "festival":
        fd, say_file = tempfile.mkstemp()
            
        execLine = f"festival --tts {say_file}"
        with open(say_file, 'w') as f:
            f.write(str(text)) 
            f.flush()
            
            os.system(execLine)
            os.close(fd)

    if model.get("type") == "speecht5":
        file_name = hashlib.md5(text.encode()).hexdigest()
        output_filename = f"{speaker}-{file_name}.wav"

        full_file_path = os.path.join(os.path.expanduser(cache_folder), output_filename)

        if not os.path.isfile(full_file_path):

            t = threading.Thread(target=play_wav_file, kwargs={ "file_path": "complete.wav", "ext_prg": ext_prg }, daemon=True)
            t.start()

            logging.getLogger("KNZY-TTS").debug(f"Caching speach segment to {full_file_path}")
            try:
                processor = model.get("processor")
                device = model.get("device")
                tts_model = model.get("model")
                embeddings_dataset = model.get("dataset")
                vocoder = model.get("vocoder")
                speakers = model.get("speakers")
                speaker_id = speakers.get(speaker)

                # preprocess text
                inputs = processor(text=text, return_tensors="pt").to(device)
                speaker_embeddings = torch.tensor(embeddings_dataset[speaker_id]["xvector"]).unsqueeze(0).to(device)

                # generate speech with the models
                speech = tts_model.generate_speech(inputs["input_ids"], speaker_embeddings, vocoder=vocoder)

                sample_rate = 16000
                # save the generated speech to a file with 16KHz sampling rate
                sf.write(full_file_path, speech.cpu().numpy(), samplerate=sample_rate)
            except Exception:
                logging.debug(str(sys.exc_info()[0]))
                logging.debug(str(traceback.format_exc()))
                logging.error("Unable to start speech output due to an internal error")

            t.join()

        play_wav_file(full_file_path, ext_prg=ext_prg)


def play_wav_file(file_path, ext_prg=None):
    CHUNK = 1024

    if not os.path.isfile(file_path):
        file_path2 = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "data", file_path))

        if not os.path.isfile(file_path2):
            logging.error(f"File not found ({file_path}).")
            return
        
        file_path = file_path2

    if ext_prg is None:

        # Open the WAV fileprocess python
        wf = wave.open(file_path, 'rb')

        ERROR_HANDLER_FUNC = CFUNCTYPE(None, c_char_p, c_int, c_char_p, c_int, c_char_p)
        c_error_handler = ERROR_HANDLER_FUNC(py_error_handler)
        asound = cdll.LoadLibrary('libasound.so')
        asound.snd_lib_error_set_handler(c_error_handler)

        # Initialize PyAudio
        p = pyaudio.PyAudio()

        # Open a stream to play the audio
        stream = p.open(format=p.get_format_from_width(wf.getsampwidth()),
                        channels=wf.getnchannels(),
                        rate=wf.getframerate(),
                        output=True)

        # Play the audio in chunks
        data = wf.readframes(CHUNK)
        while data:
            stream.write(data)
            data = wf.readframes(CHUNK)

        # Stop and close the stream and PyAudio
        stream.stop_stream()
        stream.close()
        p.terminate()

    else:

        cmd = str(ext_prg)
        if "{FILENAME}" in cmd:
            cmd = cmd.replace("{FILENAME}", file_path)
        else:
            cmd = f"{cmd} {file_path}"

        ret_val = subprocess.call(cmd, shell=True)
        if ret_val:
            logging.debug("Play completed.")