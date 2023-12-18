# KENZY.Ai &middot; [![GitHub license](https://img.shields.io/github/license/lnxusr1/kenzy)](https://github.com/lnxusr1/kenzy/blob/master/LICENSE) ![Python Versions](https://img.shields.io/pypi/pyversions/yt2mp3.svg) ![Read the Docs](https://img.shields.io/readthedocs/kenzy) ![GitHub release (latest by date)](https://img.shields.io/github/v/release/lnxusr1/kenzy)

This project is dedicated to building a "Synthetic Human" which is called Kenzy for which we have assigned the female gender pronoun of "she". She has visual face recognition ([opencv/opencv](https://github.com/opencv/opencv)), speech transcription ([coqui](https://github.com/coqui-ai)), and speech synthesis ([festival](http://www.cstr.ed.ac.uk/projects/festival/) or [mimic3](https://github.com/MycroftAI/mimic3)).  Kenzy is written in Python and is targeted primarily at the single board computer (SBC) platforms like the [Raspberry Pi](https://www.raspberrypi.org/).

Visit our main site: [https://kenzy.ai/](https://kenzy.ai/)

Read the docs: [https://docs.kenzy.ai/](https://docs.kenzy.ai/)

## Kenzy's Architecture

Kenzy's architecture is divided into compartments.  These compartments come with two main components:  Servers and Devices.  The servers focus on communication between other compartments and devices are designed to control input and output operations.  Devices are always run within a server and a server can execute only one device.  Servers talk to other servers using HTTP/HTTPS like standard web requests making customizing the communication fairly straightforward.  The most important device is the ```kenzy.skillmanager``` which is a special type of device that collects data and provides the skill engine for reacting to inputs.

All options, configurations, and startup parameters are driven configuration files.  There are a few examples available in the repository under the examples folder.

__Python Module Overview__

| Class/Object         | Description                                                           |
| :------------------- | :-------------------------------------------------------------------- |
| kenzy.core           | Core logic with inheritable objects for each device.                  |
| kenzy.extras         | Extra functions for UPNP/SSDP and other features.                     |
| kenzy.skillmanager   | Core skill manager (a.k.a. "The Brain")                               |
| kenzy.image          | Object/Face detection processing video capture (previously "Watcher") |
| kenzy.tts            | Text-to-speech models processing audio-output (previously "Speaker")  |
| kenzy.stt            | Speech-to-text models processing audio-input (previously "Listener")  |

## Installation

### Using the Installation Script

The quickest and easiest way to install Kenzy is to use our installation script:

```
wget -q -O install.sh https://kenzy.ai/installer && sh install.sh
```

Running the script exactly as shown above will install Kenzy and all components.  If you want to be more selective you can add options as follows:

* ```-b``` = Install brain dependencies
* ```-l``` = Install listener dependencies
* ```-s``` = Install speaker dependencies
* ```-w``` = Install watcher dependencies
* ```-p``` = Install panel dependencies
* ```-v [PATH]``` = Python virtual environment path (will create new if does not already exist)

Installer script has been tested on Ubuntu 22.04+, Debian Buster, and Raspberry Pi OS (Buster).

### Manual Installation

Kenzy is available through pip, but to use the built-in devices there are a few extra libraries you may require.  Please visit the [Basic Install](https://docs.kenzy.ai/en/latest/installation.basic/) page for more details.  

```
# Install PIP (Python package manager) if not already installed
sudo apt-get -y install python3-pip

# Install the required system packages
sudo apt-get -y install \
  python3-fann2 \
  python3-pyaudio \
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
sudo apt-get -y install python3-venv
mkdir -p ~/kenzy
cd ~/kenzy
python3 -m venv ./.venv --system-site-packages
source ./.venv/bin/activate

# Install the required build libraries
python3 -m pip install scikit-build 

# Install core required runtime libraries
python3 -m pip install urllib3 \
  requests \
  netifaces \
  padatious

# Install libraries for SpeakerDevice (Required only if using ```mimic3``` in place of festival)
python3 -m pip install mycroft-mimic3-tts[all]

# Install optional libraries for WatcherDevice
python3 -m pip install opencv-contrib-python \
  kenzy-image

# Install optional libraries for ListenerDevice
python3 -m pip install --upgrade numpy \
  webrtcvad \
  torch \
  torchaudio \
  sentencepiece \
  transformers \
  soundfile

# If you have trouble with pyaudio then you may want try to upgrade it
python3 -m pip install --upgrade pyaudio

# Install the kenzy module
python3 -m pip install kenzy
```

To start execute as follows:
```
python3 -m kenzy
```
You can disable one or more of the built-in devices or containers with ```--disable-builtin-[speaker, watcher, listener, brain, container]```.  Use the ```--help``` option for full listing of command line options including specifying a custom configuration file.

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


## Offline Mode

Set the following environment variables before starting the program to use the models offline:

```
TRANSFORMERS_OFFLINE=1 HF_DATASETS_OFFLINE=1
```

-----

## Help &amp; Support
Help and additional details is available at [https://kenzy.ai](https://kenzy.ai)
