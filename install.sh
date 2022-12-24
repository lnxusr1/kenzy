#!/bin/bash

# exit when any command fails
set -e
trap '[ $? -eq 0 ] && exit 0 || echo "$0 FAILED."' EXIT

brain=0;
speaker=0;
listener=0;
watcher=0;
kasaplug=0;
panel=0;

while getopts bslwkpv: flag
do
    case "${flag}" in
        b) brain=1;;
        s) speaker=1;;
        l) listener=1;;
        w) watcher=1;;
        k) kasaplug=1;;
        p) panel=1;;
        v) virtenv="${OPTARG}";;
    esac
done

if [ $brain -eq 0 ] && [ $speaker -eq 0 ] && [ $listener -eq 0 ] && [ $watcher -eq 0 ] && [ $kasaplug -eq 0 ] && [ $panel -eq 0 ]; then
    # Assume everything is to be installed
    brain=1;
    speaker=1;
    listener=1;
    watcher=1;
    kasaplug=1;
    panel=1;
fi

pythonCmd="python3"
if [ -z $virtenv ] || [ "${virtenv}" = "" ]; then
    read -p "Python Virtual Environment path: " virtenv
    if [ -z $virtenv ] || [ "${virtenv}" = "" ]; then
        echo "No Python virtual environment selected.  Will use system global."
        pythonCmd="sudo python3"
    fi
else
    if [ "${virtenv}" = "-" ]; then
        virtenv=""
        pythonCmd="sudo python3"
    fi
fi

echo "Installing base packages..."
sudo apt-get update;

sudo apt-get -y install \
    python3-pip \
    python3-dev \
    cmake \
    swig \
    python3-venv;

if [ "$virtenv" != "" ]; then
    if [ ! -d $virtenv ]; then
        echo "Setting up virtual environment...";
        python3 -m venv $virtenv --system-site-packages;
    fi

    echo "Activating virtual environment.";
    . $virtenv/bin/activate;
    echo "Virtual environment activated.";
fi

$pythonCmd -m pip install --upgrade \
    numpy \
    scikit-build \
    urllib3 \
    requests \
    netifaces;

if [ $brain -eq 1 ]; then
    echo "Installing libraries for brain module...";
    sudo apt-get -y install \
        python3-fann2;

    echo "Checking FANN implementation...";
    arch=`gcc -dumpmachine`;
    dfann=/usr/lib/$arch/libdoublefann.so.2;

    if [ -f "$dfann" ] && [ ! -f /usr/local/lib/libdoublefann.so ]; then
        findFann=`find /usr/lib/ | grep "doublefann" | wc -l`
        if [ "$findFann" = "0" ]; then
            sudo ln -s $dfann /usr/local/lib/libdoublefann.so 2>> /dev/null;
        fi
    fi

    $pythonCmd -m pip install --upgrade \
        padatious;

    echo "brain module installed.";
fi

if [ $watcher -eq 1 ]; then
    echo "Installing libraries for watcher module...";
    $pythonCmd -m pip install --upgrade \
        opencv-contrib-python \
        Pillow;
    echo "watcher module installed.";
fi

if [ $kasaplug -eq 1 ]; then
    echo "Installing libraries for kasaplug module...";
    $pythonCmd -m pip install --upgrade \
        asyncio \
        python-kasa;
    echo "kasaplug module installed.";
fi

if [ $panel -eq 1 ]; then
    echo "Installing libraries for PyQt5 Panels...";
    sudo apt-get -y install \
        python3-pyqt5;
    echo "PyQt5 installed successfully.";
fi

if [ $speaker -eq 1 ]; then
    echo "Installing libraries for speaker module...";
    sudo apt-get -y install \
        libespeak-ng1 \
        festival \
        festvox-us-slt-hts;

    #$pythonCmd -m pip install --upgrade mycroft-mimic3-tts[all];
    echo "speaker module installed.";
fi

if [ $listener -eq 1 ]; then
    echo "Installing libraries for listener module...";
    sudo apt-get -y install \
        python3-pyaudio \
        libportaudio2 \
        portaudio19-dev \
        libasound2-dev \
        libatlas-base-dev;

    $pythonCmd -m pip install --upgrade \
        webrtcvad;

    machine=`uname -m`
    if [ "${machine}" = "armv7l" ]; then
        echo "It appears you are using an ARM processor.  Attempting to install STT manually."
        wget -O stt-1.4.0-cp39-cp39-linux_armv7l.whl https://github.com/coqui-ai/STT/releases/download/v1.4.0/stt-1.4.0-cp39-cp39-linux_armv7l.whl;
        $pythonCmd -m pip install stt-1.4.0-cp39-cp39-linux_armv7l.whl;
    else
        $pythonCmd -m pip install --upgrade \
            stt;
    fi

    echo "listener module installed.";
fi

echo "Installing kenzy module...";
$pythonCmd -m pip install --upgrade kenzy;
echo "Kenzy module installed.";

if [ $listener -eq 1 ]; then
    echo "Downloading models for listener...";
    $pythonCmd -m kenzy --download-models;
    echo "Listener models downloaded";

    # Force upgrade on pyaudio but don't fail if it doesn't work
    set +e
    echo "Checking for upgrade on PyAudio...";
    $pythonCmd -m pip install --upgrade pyaudio 2>> /dev/null;
    if [ $? -ne 0 ]; then
        echo "PyAudio not upgraded, but it's probably okay to ignore this error.";
    fi
    set -e

fi

$pythonCmd -m compileall
if [ $? -ne 0 ]; then
    echo "Unable to compile libraries."
fi

echo "Installation completed successfully.";

echo "";
echo "";

if [ -z $virtenv ] || [ "${virtenv}" = "" ]; then
    echo "To get started enter the following:";
    echo "  python3 -m kenzy";
else
    echo "To get started you need to activate your virtual environment:";
    echo "  source ${virtenv}/bin/activate";
    echo "";
    echo "Once activated you can start Kenzy with the following:";
    echo "  python3 -m kenzy";
fi

echo "";