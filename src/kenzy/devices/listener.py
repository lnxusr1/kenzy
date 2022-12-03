import os
import logging
import numpy as np
import time
from kenzy.shared import threaded, py_error_handler
from kenzy import GenericDevice
import pyaudio
import queue
import webrtcvad
import collections
import stt
import sys
import traceback
from ctypes import CFUNCTYPE, cdll, c_char_p, c_int


class Listener(GenericDevice):
    """
    Listener device to capture audio from microphone and convert any speech to text and send to callback method.
    """
    
    def __init__(self, **kwargs):
        """
        Listener Initialization

        Args:
            parent (object): Containing object's reference.  Normally this would be the device container. (optional)
            speechModel (str):  Path and filename of Coqui Speech Model file.  If not set then listener will do a 
                basic seach for the PBMM or TFLite file.
            speechScorer (str):  Path and filename of Coqui Scorer file.  Okay for this to be None as scorer file is not required.
            audioChannels (int):  Audio channels for audio source.  VAD requires this to be 1 channel.
            audioSampleRate (int): Audio sample rate of audio source.  VAD requires this to be 16000.
            vadAggressiveness (int): Voice Activity Detection (VAD) aggressiveness for filtering noise.  Accepts 1 thru 3.
            speechRatio (float): Must be between 0 and 1 as a decimal
            speechBufferSize (int): Buffer size for speech frames
            speechBufferPadding (int): Padding, in milliseconds, of speech frames
            audioDeviceIndex (int): Listening device index number.  If not set then will use default audio capture device.
            callback (function): Callback function for which to send capture text    
        """

        # Local variable instantiation and initialization
        self.type = "LISTENER"
        self.logger = logging.getLogger(kwargs.get("nickname", self.type))

        self.stream = None
        self.thread = None
        self._isRunning = False 
        self._isAudioOut = False 
        
        from kenzy import __version__
        self.version = __version__
        self._packageName = "kenzy"

        super(Listener, self).__init__(**kwargs)

    def updateSettings(self):
        """
        Updates the settings based on values in self.args
        """

        self.parent = self.args.get("parent")
        self.speechModel = self.args.get("speechModel")                         # Speech Model file.  Ideally this could be searched for in a default location
        self.speechScorer = self.args.get("speechScorer")                       # Scorer file.  Okay for this to be None as scorer file is not required
        self.audioChannels = self.args.get("audioChannels", 1)                  # VAD requires this to be 1 channel
        self.audioSampleRate = self.args.get("audioSampleRate", 16000)          # VAD requires this to be 16000
        self.vadAggressiveness = self.args.get("vadAggressiveness", 1)          # VAD accepts 1 thru 3
        self.speechRatio = self.args.get("speechRatio", 0.75)                   # Must be between 0 and 1 as a decimal
        self.speechBufferSize = self.args.get("speechBufferSize", 50)           # Buffer size for speech frames
        self.speechBufferPadding = self.args.get("speechBufferPadding", 350)    # Padding, in milliseconds, of speech frames
        self.audioDeviceIndex = self.args.get("audioDeviceIndex")               # Device by index as it applies to PyAudio
        self._callbackHandler = self.args.get("callback")                       # Callback function accepts two positional args (Type, Text)

        if self.speechModel is None:
            # Search for speech model?
            self.logger.info("Speech model not specified.  Attempting to use defaults.")
            local_path = os.path.join(os.path.expanduser("~/.kenzy"), "data", "models", "speech")
            os.makedirs(local_path, exist_ok=True)
            
            files = os.listdir(local_path)
            files = sorted(files, reverse=True)  # Very poor attempt to get the latest version of the model if multiple exist.
            bFoundPBMM = False 
            bFoundTFLITE = False
            for file in files:
                if not bFoundTFLITE:
                    if file.endswith("model.tflite"):
                        self.speechModel = os.path.abspath(os.path.join(local_path, file))
                        self.logger.debug("Using speech model from " + str(self.speechModel))
                        bFoundTFLITE = True

                if not bFoundTFLITE and not bFoundPBMM:
                    if file.endswith("model.pbmm"):
                        self.speechModel = os.path.abspath(os.path.join(local_path, file))
                        self.logger.debug("Using speech model from " + str(self.speechModel))
                        bFoundPBMM = True
                        
                if self.speechScorer is None:
                    if file.endswith("huge-vocabulary.scorer"):
                        self.speechScorer = os.path.abspath(os.path.join(local_path, file))
                        self.logger.debug("Using speech scorer from " + str(self.speechScorer))
        
            if bFoundPBMM and bFoundTFLITE:
                self.logger.warning("Found both PBMM and TFLite deepspeech models.")
                self.logger.warning("Defaulting to TFLITE model.")
                
        if self.speechModel is None:
            # FIXME: Should we try to download the models if they don't exist?
            raise Exception("Invalid speech model.  Unable to start listener.")

        return True 

    @threaded
    def _doCallback(self, inData):
        """
        Calls the specified callback as a thread to keep from blocking audio device listening

        Args:
            text (str):  Text to send to callback function
        
        Returns:
            (thread):  The thread on which the callback is created to be sent to avoid blocking calls.
        """

        try:
            if self._callbackHandler is not None:
                self._callbackHandler("AUDIO_INPUT", inData)
        except Exception:
            pass
        
        return

    @threaded
    def _readFromMic(self):
        """
        Opens audio device for listening and processing speech to text
        
        Returns:
            (thread):  The thread created for the listener while listening for incoming speech.
        """
    
        buffer_queue = queue.Queue()    # Buffer queue for incoming frames of audio
        self._isRunning = True   # Reset to True to insure we can successfully start
    
        def proxy_callback(in_data, frame_count, time_info, status):
            """Callback for the audio capture which adds the incoming audio frames to the buffer queue"""
            
            # Save captured frames to buffer
            buffer_queue.put(in_data)
            
            # Tell the caller that it can continue capturing frames
            return (None, pyaudio.paContinue)
    
        # Using a collections queue to enable fast response to processing items.
        # The collections class is simply faster at handling this data than a simple dict or array.
        # The size of the buffer is the length of the padding and thereby those chunks of audio.
        ring_buffer = collections.deque(
            maxlen=self.speechBufferPadding // (1000 * int(self.audioSampleRate / float(self.speechBufferSize)) // self.audioSampleRate))
    
        # Set up C lib error handler for Alsa programs to trap errors from Alsa spin up
        # with SilenceStream(sys.stderr, log_file="/dev/null"):
        _model = stt.Model(self.speechModel)
        if self.speechScorer is not None:
            _model.enableExternalScorer(self.speechScorer)
            
        _vad = webrtcvad.Vad(self.vadAggressiveness)
        
        ERROR_HANDLER_FUNC = CFUNCTYPE(None, c_char_p, c_int, c_char_p, c_int, c_char_p)
        c_error_handler = ERROR_HANDLER_FUNC(py_error_handler)
        asound = cdll.LoadLibrary('libasound.so')
        asound.snd_lib_error_set_handler(c_error_handler)
        
        _audio_device = pyaudio.PyAudio()
        
        # Open a stream on the audio device for reading frames
        try:
            self.stream = _audio_device.open(
                format=pyaudio.paInt16,
                channels=self.audioChannels,
                rate=self.audioSampleRate,
                input=True,
                frames_per_buffer=int(self.audioSampleRate / float(self.speechBufferSize)),
                input_device_index=self.audioDeviceIndex,
                stream_callback=proxy_callback)
            
            self.stream.start_stream()                               # Open audio device stream
        except Exception:
            logging.debug(str(sys.exc_info()[0]))
            logging.debug(str(traceback.format_exc()))
            self.logger.error("Unable to read from listener device.")
            self._isRunning = False
            self.stop()
            return False 

        # Context of audio frames is used to better identify the spoken words.
        stream_context = _model.createStream()
        
        # Used to flag whether we are above or below the ratio threshold set for speech frames to total frames
        triggered = False
        
        self.logger.info("Started")
        
        # We will loop looking for incoming audio until the KILL_SWITCH is set to True
        while self._isRunning:
    
            # Get current data in buffer as an audio frame
            frame = buffer_queue.get()
    
            # A lot of the following code was pulled from examples on DeepSpeech (which has since become Coqui)
            # https://github.com/mozilla/DeepSpeech-examples/blob/r0.7/mic_vad_streaming/mic_vad_streaming.py
            
            # Important note that the frame lengths must be specific sizes for VAD detection to work.
            # Voice Activity Detection (VAD) also expects single channel input at specific rates.
            # Highly recommend reading up on webrtcvad() before adjusting any of this.
            
            # We also skip this process if we are actively sending audio to the output device to avoid
            # looping and thus listening to ourselves.
            if len(frame) >= 640 and not self._isAudioOut:
                
                # Bool to determine if this frame includes speech.
                # This only determines if the frame has speech, it does not translate to text.
                is_speech = _vad.is_speech(frame, self.audioSampleRate)

                # Trigger is set for first frame that contains speech and remains triggered until 
                # we fall below the allowed ratio of speech frames to total frames
    
                if not triggered:
    
                    # Save the frame to the buffer along with an indication of if it is speech (or not)
                    ring_buffer.append((frame, is_speech))
    
                    # Get the number of frames with speech in them
                    num_voiced = len([f for f, speech in ring_buffer if speech])
    
                    # Compare frames with speech to the expected number of frames with speech
                    if num_voiced > self.speechRatio * ring_buffer.maxlen:
                        
                        # We have more speech than the ratio so we start listening
                        triggered = True
    
                        # Feed data into the deepspeech model for determing the words used
                        for f in ring_buffer:
                            stream_context.feedAudioContent(np.frombuffer(f[0], np.int16))
    
                        # Since we've now fed every frame in the buffer to the deepspeech model
                        # we no longer need the frames collected up to this point
                        ring_buffer.clear()
            
                else:
                    # We only get here after we've identified we have enough frames to cross the threshold
                    # for the supplied ratio of speech to total frames.  Thus we can safely keep feeding
                    # incoming frames into the deepspeech model until we fall below the threshold again.
                    
                    # Feed to deepspeech model the incoming frame
                    stream_context.feedAudioContent(np.frombuffer(frame, np.int16))
    
                    # Save to ring buffer for calculating the ratio of speech to total frames with speech
                    ring_buffer.append((frame, is_speech))
                    
                    # We have a full collection of frames so now we loop through them to recalculate our total
                    # number of non-spoken frames (as I pulled from an example this could easily be stated as
                    # the inverse of the calculation in the code block above)
                    num_unvoiced = len([f for f, speech in ring_buffer if not speech])
    
                    # Compare our calculated value with the ratio.  In this case we're doing the opposite
                    # of the calculation in the previous code block by looking for frames without speech
                    if num_unvoiced > self.speechRatio * ring_buffer.maxlen:
                        
                        # We have fallen below the threshold for speech per frame ratio
                        triggered = False
                        
                        # Let's see if we heard anything that can be translated to words.
                        # This is the invocation of the deepspeech's primary STT logic.
                        # Note that this is outside the kill_switch block just to insure that all the
                        # buffers are cleaned and closed properly.  (Arguably this is not needed if killed)
                        text = str(stream_context.finishStream())
    
                        # We've completed the hard part.  Now let's just clean up.
                        if self._isRunning:
                            
                            # We'll only process if the text if there is a real value AND we're not already processing something.
                            # We don't block the processing of incoming audio though, we just ignore it if we're processing data.
                            if text.strip() != "":
    
                                self.logger.info("HEARD " + text)
                                self._doCallback(text)
                                
                            stream_context = _model.createStream()  # Create a fresh new context
    
                        ring_buffer.clear()  # Clear the ring buffer as we've crossed the threshold again
    
        self.logger.debug("Stopping streams")        
        self.stream.stop_stream()                          # Stop audio device stream
        self.stream.close()                                # Close audio device stream
        self.logger.debug("Streams stopped")

    def accepts(self):
        return ["start", "stop", "audioOutStart", "audioOutEnd"]
        
    def isRunning(self):
        """
        Identifies if the device is actively running.

        Returns:
            (bool):  True if running; False if not running.
        """

        return self._isRunning
    
    def audioOutStart(self, httpRequest=None):
        """
        Sets the device to temporarily stop capturing audio (used to prevent listening to the speaker device output).

        Args:
            httpRequest (KHTTPHandler): Handler for inbound request.  Not used.

        Returns:
            (bool):  True on success else will raise an exception.
        """

        self._isAudioOut = True
        return True
    
    def audioOutEnd(self, httpRequest=None):
        """
        Sets the device to start capturing audio again after an audioOutStart message.

        Args:
            httpRequest (KHTTPHandler): Handler for inbound request.  Not used.

        Returns:
            (bool):  True on success else will raise an exception.
        """

        self._isAudioOut = False
        return True
    
    def stop(self, httpRequest=None):
        """
        Stops the listener and any active audio streams
        
        Args:
            httpRequest (KHTTPHandler): Handler for inbound request.  Not used.

        Returns:
            (bool):  True on success else will raise an exception.
        """

        if not self._isRunning:
            return True 

        self._isRunning = False
        if self.thread is not None:
            self.thread.join()
            
        self.logger.info("Stopped")
        return True
        
    def start(self, httpRequest=None, useThreads=True):
        """
        Starts the listener to listen to the default audio device

        Args:
            httpRequest (KHTTPHandler): Handler for inbound request.  Not used.
            useThreads (bool):  Indicates if the brain should be started on a new thread.
        
        Returns:
            (bool):  True on success else will raise an exception.
        """

        if self._isRunning:
            return True 
        
        self.thread = self._readFromMic()
        if not useThreads:
            self.wait()
            
        return True
    
    def wait(self, seconds=0):
        """
        Waits for any active listeners to complete before closing
        
        Args:
            seconds (int):  Number of seconds to wait before calling the "stop()" function
            
        Returns:
            (bool):  True on success else will raise an exception.
        """
        
        if not self._isRunning:
            return True 
        
        if seconds > 0:
            if self.thread is not None:
                time.sleep(seconds)
                self.stop()
        
        else:
            if self.thread is not None:
                self.thread.join()
            
        return True