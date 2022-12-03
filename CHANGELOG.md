# Changelog

All notable changes to this project will be documented in this file.

## [0.9.9]

### Modified

- Listner error trapping for invalid audio devices to report stopped status on failure
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