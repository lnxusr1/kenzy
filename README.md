# KENZY.Ai &middot; [![GitHub license](https://img.shields.io/github/license/lnxusr1/kenzy)](https://github.com/lnxusr1/kenzy/blob/master/LICENSE) ![Python Versions](https://img.shields.io/pypi/pyversions/yt2mp3.svg) ![Read the Docs](https://img.shields.io/readthedocs/kenzy) ![GitHub release (latest by date)](https://img.shields.io/github/v/release/lnxusr1/kenzy)

This project is dedicated to building a "Synthetic Human" which is called Kenzy for which we have assigned the female gender pronoun of "she". She has intent determination ([padatious](https://github.com/MycroftAI/padatious)) visual face recognition ([opencv/opencv](https://github.com/opencv/opencv)), speech transcription ([whisper](https://openai.com/research/whisper)), and speech synthesis ([speecht5](https://github.com/microsoft/SpeechT5)/[festival](http://www.cstr.ed.ac.uk/projects/festival/).  Kenzy is written in Python and is targeted primarily at locally hosted home devices.

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

-----

## Installation

### Using the Installation Script

The quickest and easiest way to install Kenzy is to use our installation script:

```bash
wget -q -O install.sh https://kenzy.ai/installer && sh install.sh
```

Running the script exactly as shown above will install Kenzy and all components.  If you want to be more selective you can add options as follows:

* ```-b``` = Install skill manager dependencies (formerly the "Brain")
* ```-l``` = Install stt dependencies (formerly the "Listener")
* ```-s``` = Install tts dependencies (formerly the "Speaker")
* ```-w``` = Install image dependencies (formerly the "Watcher")
* ```-v [PATH]``` = Python virtual environment path (will create new if does not already exist)

Installer script has been tested on Ubuntu 22.04+, Debian Buster, and Raspberry Pi OS (Buster).

### Manual Installation

Kenzy is available through pip, but to use the built-in devices there are a few extra libraries you may require.  Please visit the [Basic Install](https://docs.kenzy.ai/en/latest/installation.basic/) page for more details.  

```bash
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
  padatious

# Install optional libraries for WatcherDevice
python3 -m pip install --upgrade \
  opencv-contrib-python \
  yolov7detect==1.0.1 \
  face_recognition \
  numpy;

# Install optional libraries for ListenerDevice and SpeakerDevice

sudo apt-get -y install \
  libespeak-ng1 \
  festival \
  festvox-us-slt-hts \
  python3-pyaudio \
  libportaudio2 \
  portaudio19-dev \
  libasound2-dev \
  libatlas-base-dev;

python3 -m pip install --upgrade \
  PyAudio>=0.2.13 \
  soundfile \
  wave \
  torch \
  fsspec==2023.9.2 \
  transformers==4.31.0 \
  datasets==2.14.3 \
  webrtcvad \
  sentencepiece==0.1.99;

# If you have trouble with pyaudio then you should insure it is upgraded with:
python3 -m pip install --upgrade pyaudio

# Install the kenzy module
python3 -m pip install kenzy
```
__NOTE:__ The installation of OpenCV is required when using the watcher device.  This may take a while on the Raspberry Pi OS as it has to recompile some of the libraries.  Patience is required here as the spinner icon appeared to get stuck several times in our tests... so just let it run until it completes.  If it encounters a problem then it will print out the error for additional troubleshooting.  

If you prefer not to wait then you can install the opencv package that comes with most distributions however this version does not support facial recognition.  To use the package instead then issue ```apt-get install python3-opencv``` and remove the ```opencv-contrib-python``` from the pip package list above.  (This will spead up the installation time significantly on the Raspberry Pi at the cost of functionality.)

-----

## Troubleshooting: "Cannot find FANN libs"
If you encounter an error trying to install the kenzy module on the Raspberry Pi then you may need to add a symlink to the library FANN library. This is due to a bug/miss in the "find_fann" function within the Python FANN2 library as it doesn't look for the ARM architecture out-of-the-box.  To fix it run the following:

### Raspberry Pi (ARM)
```bash
sudo ln -s /usr/lib/arm-linux-gnueabihf/libdoublefann.so.2 /usr/local/lib/libdoublefann.so
```

### Ubuntu 22.04 LTS (x86_64)
```bash
sudo ln -s /usr/lib/x86_64-linux-gnu/libdoublefann.so.2 /usr/local/lib/libdoublefann.so
```

-----

## Starting Up
You can execute Kenzy directly as a module.  To do so try the following:

```bash
python3 -m kenzy --config CONFIG_FILE
```
Use the ```--help``` option for full listing of command line options including specifying a [custom configuration](https://docs.kenzy.ai/en/latest/kenzy.config/) file.

## Web Control Panel

If everything is working properly you should be able to point your device to the web control panel running on the __Brain__ engine to test it out.  The default URL is:

__&raquo; [http://localhost:9700/](http://localhost:9700/)__


-----

## Help &amp; Support
Help and additional details is available at [https://kenzy.ai](https://kenzy.ai)
