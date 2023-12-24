# Installation

## Using the Installation Script

The quickest and easiest way to install Kenzy is to use our installation script:

```
wget -q -O install.sh https://kenzy.ai/installer && sh install.sh
```

Running the script exactly as shown above will install Kenzy and all components.  If you want to be more selective you can add options as follows:

* ```-b``` = Install skill manager dependencies (formerly the "Brain")
* ```-l``` = Install stt dependencies (formerly the "Listener")
* ```-s``` = Install tts dependencies (formerly the "Speaker")
* ```-w``` = Install image dependencies (formerly the "Watcher")
* ```-v [PATH]``` = Python virtual environment path (will create new if does not already exist)

Installer script has been tested on Ubuntu 22.04+, Debian Buster, and Raspberry Pi OS (Buster).

-----

## Manual Installation

Kenzy is available through pip, but to use the built-in devices there are a few extra libraries you may require.

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
```
sudo ln -s /usr/lib/arm-linux-gnueabihf/libdoublefann.so.2 /usr/local/lib/libdoublefann.so
```

### Ubuntu 22.04 LTS (x86_64)
```
sudo ln -s /usr/lib/x86_64-linux-gnu/libdoublefann.so.2 /usr/local/lib/libdoublefann.so
```
-----

## Starting Up
You can execute Kenzy directly as a module or by calling the main kenzy program directly.  To do so try the following:

```
kenzy --config CONFIG_FILE
```

Use the ```--help``` option for full listing of command line options including specifying a [custom configuration](kenzy.config.md) file.

## Web Control Panel

If everything is working properly you should be able to point your device to the web control panel running on the __Brain__ engine to test it out.  The default URL is:

__[http://localhost:9700/](http://localhost:9700/)__

-----

## Help &amp; Support
Help and additional details is available at [https://kenzy.ai](https://kenzy.ai)
