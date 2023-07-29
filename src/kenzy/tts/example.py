import os
os.environ["TRANSFORMERS_OFFLINE"] = "1"
os.environ["HF_DATASETS_OFFLINE"] = "1"

from transformers import SpeechT5Processor, SpeechT5ForTextToSpeech, SpeechT5HifiGan
from datasets import load_dataset
import torch
import random
import string
import soundfile as sf
import logging
import hashlib

#OfflineMode.enable()

logging.basicConfig(
    datefmt='%Y-%m-%d %H:%M:%S %z',
    format='%(asctime)s %(name)-12s - %(levelname)-9s - %(message)s',
    level=logging.DEBUG)

device = "cuda" if torch.cuda.is_available() else "cpu"

# load the processor
processor = SpeechT5Processor.from_pretrained("microsoft/speecht5_tts")
#processor = SpeechT5Processor.from_pretrained("/home/lnxusr1/.cache/huggingface/hub/models--microsoft--speecht5_tts/snapshots/1e7b8f56602ea9f0bf6878666e075115e627c88b")
# load the model
model = SpeechT5ForTextToSpeech.from_pretrained("microsoft/speecht5_tts").to(device)
#model = SpeechT5ForTextToSpeech.from_pretrained("/home/lnxusr1/.cache/huggingface/hub/models--microsoft--speecht5_tts/snapshots/1e7b8f56602ea9f0bf6878666e075115e627c88b").to(device)
# load the vocoder, that is the voice encoder
vocoder = SpeechT5HifiGan.from_pretrained("microsoft/speecht5_hifigan").to(device)
#vocoder = SpeechT5HifiGan.from_pretrained("/home/lnxusr1/.cache/huggingface/hub/models--microsoft--speecht5_hifigan/snapshots/bb6f429406e86a9992357a972c0698b22043307d").to(device)
# we load this dataset to get the speaker embeddings
embeddings_dataset = load_dataset("Matthijs/cmu-arctic-xvectors", split="validation")
#embeddings_dataset = load_dataset("json", data_files="/home/lnxusr1/.cache/huggingface/datasets/Matthijs___cmu-arctic-xvectors/default/0.0.1/a62fea1f9415e240301ea0042ffad2a3aadf4d1caa7f9a8d9512d631723e781f/dataset_info.json", split="train")

speakers = {
    'awb': 0,     # Scottish male
    'bdl': 1138,  # US male
    'clb': 2271,  # US female
    'jmk': 3403,  # Canadian male
    'ksp': 4535,  # Indian male
    'rms': 5667,  # US male
    'slt': 6799   # US female
}

def save_text_to_speech(text, speaker=None):
    # preprocess text
    inputs = processor(text=text, return_tensors="pt").to(device)
    if speaker is not None:
        # load xvector containing speaker's voice characteristics from a dataset
        speaker_embeddings = torch.tensor(embeddings_dataset[speaker]["xvector"]).unsqueeze(0).to(device)
    else:
        # random vector, meaning a random voice
        speaker_embeddings = torch.randn((1, 512)).to(device)
    # generate speech with the models
    speech = model.generate_speech(inputs["input_ids"], speaker_embeddings, vocoder=vocoder)

    file_name = hashlib.md5(text.encode()).hexdigest()
    if speaker is not None:
     
        # if we have a speaker, we use the speaker's ID in the filename
        output_filename = f"{speaker}-{file_name}.wav"
    else:
        # if we don't have a speaker, we use a random string in the filename
        random_str = ''.join(random.sample(string.ascii_letters+string.digits, k=5))
        output_filename = f"{random_str}-{file_name}.wav"
    # save the generated speech to a file with 16KHz sampling rate
    sf.write(output_filename, speech.cpu().numpy(), samplerate=16000)
    # return the filename for reference
    return output_filename

file_name = save_text_to_speech("Hello, my name is Kenzy.  How are you today?", speaker=speakers["rms"])
import time
start = time.time()
file_name = save_text_to_speech("Hello, my name is Kenzy.  How are you today?", speaker=speakers["slt"])
end = time.time()
print("Completed in ", (end - start), "seconds")
print(file_name)