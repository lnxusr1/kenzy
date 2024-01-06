# Changelog

All notable changes to this project will be documented in this file.

## [2.0.3]

### Added

- Added a default configuration for the base kenzy startup (saved to .kenzy/config.yml).
- Core support for versioning skills. (use `self._version` to set version number).
- Added `--skip` and `--only` options to skip or include device configs in provided file.
- New skill option for WeatherSkill (requires API key from [openweathermap.org](http://openweathermap.org))
- Added option to set default value when getting settings in skills

### Modified

- Changed startup to use Multiprocessing instead of Threads for each device main runtime
- Added ThreadingMixIn to HTTPServer (oops!)
- Set default of "Kenzy's Room" and "Kenzy's Group" for location and group respectively
- Improved responses to the "How are you" question.

## [2.0.2]

### Modified

- Fixed bug in skillmanager.device.collect
- Fixed bug in core.KenzyRequestHandler.log_message
- Fixed bug in *Cameras* count on dashboard

## [2.0.1]

### Added

- Settings handler for consistency when customizing per device settings
- GPUs can be leveraged for torch and cuda enabled models
- Added options for saving video of detected people
- Directly incorporated kenzy_image into kenzy.image.core.detector
- Added reloadFaces logic to kenzy.image.detector (formerly of the kenzy-image package)
- Added voice activation with configurable timeout
- Added multi-model support for speak-to-text
- Added configurable timeout for SSDP client requests
- Added extras helpers to extract numbers from strings and convert numbers to english words.
- Added clean text routine for supporting the rich output from OpenAi's Whisper model
- Basic support for simultaneous actions (such as two listener+speakers in two rooms connected to same skillmanager)
- Object recognition, Face detection, and Face recognition with optimizations to minimize processing time with support for multiple models
- Configurable saving of videos based on object detection alerts

### Modified

- Settings/Configuration files can now be stored in JSON or YAML files
- Moved watcher to ```kenzy.image.device.VideoReader```
- Moved listener to ```kenzy.stt.device.AudioReader```
- Moved speaker to ```kenzy.tts.device.AudioWriter```
- Restructured devices to allow for direct calls for "main" in each of image, stt, and tts
- Split out detector/creator processes for each of hte core functions into their own modules (e.g. kenzy.image.detector, kenzy.stt.detector, etc.)
- Moved all devices to their own HTTP server module when run as clients
- Fixed the UPNP logic so that it honors the full UPNP spec for control interface lookups
- Updated skills intent function signature to include ```**kwargs``` for additional values like raw text captured
- Fixed the context inclusion and usage for action/response activities (uses "location" for relative responses)
- Completely overhauled dashboard

### Removed

- Dropped support for PyQt5 panels
- Dropped direct support for Kasa smart switch/plug devices
- Dropped unnecessary libraries (urllib3, netifaces)
- Dropped support for MyCroft libraries "mimic3" (created forked version of padatious for future internal support)
- Dropped direct support for Raspberry Pi due to hardware limitations

## [1.0.0]

### Modified

- (MINOR) Fixed bug in autoStart conditions for devices preventing devices from honoring the setting when set to ```False```
- Moved RaspiPanel into "panels" module
- Set the running app to be PyQt5 specific
- Adjusted the startup arguments for GenericContainer to be non-specific
- Fixed build cleanup process
- Set the PyQt5 example panel to be disabled by default (but available to 'start' in web UI)

## [0.9.9]

### Modified

- Listener error trapping for invalid audio devices to report stopped status on failure
- Watcher error trapping for invalid camera devices to report stopped status on failure
- GenericContainer now saves core init() args to ```self.config``` and initialize() args to ```self.args```

## [0.9.8]

### Added

- Downloadable installer script
- Installer script documentation
- Logo in docs

### Modified

- (CRITICAL) Fixed skill inclusion breaking runtime due to missing "create_skill()" attributes
- Cleaned up documentation on inclusion of libraries (added python3-venv and removed traceback)
- Corrected documentation on PyAudio library installation
- (CRITICAL) Fixed inclusion of missing files in PyPi build

- (Sorry about the version increments... still getting use to PyPi.org)

## [0.9.2]

### Added

- Added ```nickname``` option to devices/containers

### Modified

- PyPi integrations updated and streamlined build
- Modified versioning storage/processing
- Updated to a basic README for PyPi download page
- Multiple bugs fixed in KasaPlug for local, direct plug access
- Bug fix for isAlive() to is_alive()

## [0.9.1]

### Added

- Dependency on stt (a.k.a. "coqui" which is a replacement for deepspeech)
- Added new parameters for Speaker to be able to integrate with mimic3

### Modified

- Renamed all objects to support "kenzy" (and related variants)
- Updated "ask" function to start timeout after the originating utterence ends (rather than when it starts)
- Documentation for installing on Ubuntu 22.04 LTS (pyaudio and libfann source package installation workarounds)
- Download Models option now defaults to tflite format and pulls the Coqui base models from the Coqui Model Zoo
- Moved all request/response processing into Skills and removed hardcoded responses
- Removed hard dependency on padatious library
- Fixed bug in skills processing for multiple intents
- Updated device callbacks to use the GenericDevice naming convention
- Updated device settings to allow for store/update on the fly
- Updated container settings to allow for store/update on the fly
- Adjusted where version information is stored

### Removed

- Dependecy on deepspeech
- Documentation dependency on padatious (libfann related issues for auto-build in readthedocs API)