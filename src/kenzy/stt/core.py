# from ctypes import CFUNCTYPE, cdll, c_char_p, c_int
from transformers import AutoProcessor, AutoModelForSpeechSeq2Seq
import logging


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


def speech_model(model_name="openai/whisper-tiny.en"):
    processor = AutoProcessor.from_pretrained(model_name)
    model = AutoModelForSpeechSeq2Seq.from_pretrained(model_name)
    model.config.forced_decoder_ids = None

    return processor, model