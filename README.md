# KENZY.Ai &middot; [![GitHub license](https://img.shields.io/github/license/lnxusr1/kenzy)](https://github.com/lnxusr1/kenzy/blob/master/LICENSE) ![Python Versions](https://img.shields.io/pypi/pyversions/yt2mp3.svg) ![Read the Docs](https://img.shields.io/readthedocs/kenzy) ![GitHub release (latest by date)](https://img.shields.io/github/v/release/lnxusr1/kenzy)

This project is dedicated to building a "Synthetic Human" which is called Kenzy for which we have assigned the female gender pronoun of "she". She has visual face recognition ([opencv/opencv](https://github.com/opencv/opencv)), speech transcription ([coqui](https://github.com/coqui-ai)), and speech synthesis ([festival](http://www.cstr.ed.ac.uk/projects/festival/) or [mimic3](https://github.com/MycroftAI/mimic3)).  Kenzy is written in Python and is targeted primarily at the single board computer (SBC) platforms like the [Raspberry Pi](https://www.raspberrypi.org/).

Visit our main site: [https://kenzy.ai/](https://kenzy.ai/)

Read the docs: [https://docs.kenzy.ai/](https://docs.kenzy.ai/)

## Kenzy's Architecture

Kenzy's architecture is divided into two main components:  Containers and Devices.  The containers focus on communication between other containers and devices are designed to control input and output operations.  The most important container is the Brain which is a special type of container as it collects data and provides the skill engine for reacting to inputs.  While a Brain does support all the methods of a normal container it is recommended to create a separate container to store all your devices.

All options, configurations, and startup parameters are driven by the configuration file saved to the following location:
```~/.kenzy/config.json```

__Python Module Overview__

| Class/Object                      | Description                      | TCP Port |
| :-------------------------------- | :------------------------------- | :------: |
| kenzy.containers.Brain            | Main service for processing I/O. | 8080     |
| kenzy.containers.DeviceContainer  | Secondary service for devices.   | 8081     |

__Python Device Module Overview__

| Class/Object              | Description                                                 |
| :------------------------ | :---------------------------------------------------------- |
| kenzy.devices.Speaker     | Audio output device for text-to-speech conversion           |
| kenzy.devices.Listener    | Microphone device for speech-to-text conversion             |
| kenzy.devices.Watcher     | Video/Camera device for object recognition                  |
| kenzy.devices.KasaDevice  | Smart plug device for Kasa devices                          |
| kenzy.panels.RaspiPanel   | Panel device designed for Raspberry Pi 7" screen @ 1024x600 |

## Installation

Kenzy is available through pip, but to use the built-in devices there are a few extra libraries you may require.  Please visit the [Basic Install](https://docs.kenzy.ai/en/latest/installation.basic/) page for more details.  

```
# Install PIP (Python package manager) if not already installed
sudo apt-get -y install python3-pip

# Install the required system packages
sudo apt-get -y install \
  python3-fann2 \
  python3-pyaudio \
  python3-pyqt5 \
  python3-dev \
  libespeak-ng1 \
  festival \
  festvox-us-slt-hts  \
  libportaudio2 \
  portaudio19-dev \
  libasound2-dev \
  libatlas-base-dev \
  cmake \
  swig

# Create your local environment and then activate it
python3 -m venv /path/to/virtual/env --system-site-packages
source /path/to/virtual/env/bin/activate

# Install the required build libraries
python3 -m pip install scikit-build 

# Install core required runtime libraries
python3 -m pip install urllib3 \
  requests \
  netifaces \
  padatious \
  traceback

# Install libraries for SpeakerDevice (Required only if using ```mimic3``` in place of festival)
python3 -m pip install mycroft-mimic3-tts[all]

# Install optional libraries for WatcherDevice
python3 -m pip install opencv-contrib-python \
  Pillow

# Install optional libraries for KasaDevice
python3 -m pip install asyncio \
  python-kasa

# Install optional libraries for ListenerDevice
python3 -m pip install --upgrade numpy \
  pyaudio \
  webrtcvad \
  stt

python3 -m pip install coqui-stt-module-manager # (For model management, not required)

# Install the kenzy module
python3 -m pip install kenzy
```

To start execute as follows:
```
python3 -m kenzy
```
You can disable one or more of the built-in devices or containers with ```--disable-builtin-[speaker, watcher, listener, panels, brain, container]```.  Use the ```--help``` option for full listing of command line options including specifying a custom configuration file.

__NOTE:__ The program will create/save a version of the configuration to ```~/.kenzy/config.json``` along with any other data elements it requires for operation.  The configuration file is fairly powerful and will allow you to add/remove devices and containers for custom configurations including 3rd party devices or custom skills.


## Troubleshooting: "Cannot find FANN libs"
If you encounter an error trying to install the kenzy module on the Raspberry Pi then you may need to add a symlink to the library FANN library. This is due to a bug/miss in the "find_fann" function within the Python FANN2 library as it doesn't look for the ARM architecture out-of-the-box.  To fix it run the following:

### Raspberry Pi (ARM)
```
sudo ln -s /usr/lib/arm-linux-gnueabihf/libdoublefann.so.2 /usr/local/lib/libdoublefann.so
```

### Ubuntu 22.04 LTS (x86_64)
```
sudo ln -s /usr/lib/x86_64-linux-gnu/libdoublefann.so.2 /usr/local/lib/libdoublefann.so
```

## Enabling Speech-to-Text

In order to enable Speech-to-Text (STT) you need to download a speech model.  You can use Coqui's model manager or use Kenzy to download one for you.  The easiest solution is likely the following command:

```
python3 -m kenzy --download-models
```

## Web Control Panel

If everything is working properly you should be able to point your device to the web control panel running on the __Brain__ engine to test it out.  The default URL is:

__&raquo; [http://localhost:8080/](http://localhost:8080/)__

-----

## Help &amp; Support
Help and additional details is available at [https://kenzy.ai](https://kenzy.ai)
