from PyQt5 import QtCore, QtGui, QtWidgets
from PyQt5.QtWidgets import QApplication, QDesktopWidget
from PyQt5.QtCore import Qt, QPoint
import os
from datetime import datetime 
import sys
import urllib
from kenzy.shared import sendHTTPRequest, upgradePackage
from kenzy.panels.shared import VideoButton, DownloadThread, Switch

from . import form_ui_simple
from . import settings_ui

from PyQt5.Qt import QPixmap, QPushButton


class DeviceItem(QtWidgets.QWidget, settings_ui.Ui_DeviceItem):
    def __init__(self):
        super(DeviceItem, self).__init__()
        self.setupUi(self)


class PanelApp(QtWidgets.QMainWindow, form_ui_simple.Ui_MainWindow):
    def __init__(self, **kwargs):
        super(PanelApp, self).__init__(kwargs.get("parent"))
        self.setupUi(self)
        
        from . import __version__, __appyear__
        self.version = __version__
        
        self._packageName = "kenzy"
        self._isRunning = False
        
        self.nickname = kwargs.get("nickname")
        self.fullScreen = kwargs.get("fullScreen", None)
        self.screenSelection = kwargs.get("screenSelection", None)
            
        self.setWindowFlags(Qt.WindowStaysOnTopHint | Qt.FramelessWindowHint)
        
        self.positionWindow()

        QtGui.QFontDatabase.addApplicationFont(os.path.join(os.path.dirname(__file__), "resources/fonts/Orbitron-VariableFont_wght.ttf"))
        QtGui.QFontDatabase.addApplicationFont(os.path.join(os.path.dirname(__file__), "resources/fonts/Wallpoet-Regular.ttf"))
        
        icon = QtGui.QIcon()
        icon.addPixmap(QtGui.QPixmap(os.path.join(os.path.dirname(__file__), "resources/images/home-solid.svg")), QtGui.QIcon.Normal, QtGui.QIcon.Off)
        self.btnHome.setIcon(icon)

        icon = QtGui.QIcon()
        icon.addPixmap(QtGui.QPixmap(os.path.join(os.path.dirname(__file__), "resources/images/camera-solid.svg")), QtGui.QIcon.Normal, QtGui.QIcon.Off)
        self.btnCams.setIcon(icon)

        icon = QtGui.QIcon()
        icon.addPixmap(QtGui.QPixmap(os.path.join(os.path.dirname(__file__), "resources/images/tools-solid.svg")), QtGui.QIcon.Normal, QtGui.QIcon.Off)
        self.btnSettings.setIcon(icon)

        self.setStyleSheet("background: rgba(255,0,0,0);")

        self.lblTime.setText("")
        self.lblDate.setText("")
        self.updateTime()
        
        self.lblName.setText("KN-Z " + self.version.replace(".", "") + __appyear__)
        
        self._timer = QtCore.QTimer()
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self.updateTime)
        self._timer.start()

        self._timerSettings = QtCore.QTimer()
        self._timerSettings.setInterval(5000)
        self._timerSettings.timeout.connect(self.monitorStatuses)
        self._timerSettings.start()
        
        self.videoLabelThread = None
        
        self.devices = []
        self.selectedDevice = None
        
        self.btnHome.clicked.connect(self.openHomeScreen)
        self.btnCams.clicked.connect(self.openCamsScreen)
        self.btnSettings.clicked.connect(self.openSettingsScreen)
        
        self.activeScreen = "home"
        self.openHomeScreen()
        
    def accepts(self):
        return ["start", "stop", "upgrade"]
        
    def isRunning(self):
        return self._isRunning
    
    def positionWindow(self):
        qtRectangle = self.frameGeometry()
        centerPoint = QDesktopWidget().availableGeometry().center()
        qtRectangle.moveCenter(centerPoint)
        self.move(qtRectangle.topLeft())
        
        desktop = QApplication.desktop()
        screenCount = desktop.screenCount()
        
        myRect = self.geometry()
            
        myWidth = myRect.width()
        myHeight = myRect.height()
        
        if self.screenSelection is None:
            for i in range(0, screenCount):
                x = desktop.screenGeometry(i)
                if x.width() == 1024 and x.height() == 600:
                    self.screenSelection = i
                    break
        
        if self.screenSelection is not None and self.screenSelection < screenCount:
            r = desktop.screenGeometry(self.screenSelection)
            self.move(QPoint(r.x(), r.y()))
            
            sWidth = int(r.width())
            sHeight = int(r.height())
            
            sX = int(r.x())
            sY = int(r.y())
            
            self.move(QPoint(sX + sWidth / 2 - myWidth / 2, sY + sHeight / 2 - myHeight / 2))
            if sWidth == 1024 and sHeight == 600:
                self.setWindowState(Qt.WindowFullScreen)
        
        if self.fullScreen is not None and isinstance(self.fullScreen, bool) and self.fullScreen:
            self.setWindowState(Qt.WindowFullScreen)
    
    def show(self):
        ret = super().show()
        self.positionWindow()
        return ret
    
    def start(self, httpRequest=None):
        self._isRunning = True
        return self.show()
    
    def stop(self, httpRequest=None):
        self._isRunning = False
        return self.hide()
    
    def upgrade(self, httpRequest=None):
        return upgradePackage(self._packageName)
        
    def updateTime(self):
        dt = datetime.now()
        self.lblDate.setText(dt.strftime("%a %b %-d"))
        self.lblTime.setText(dt.strftime("%-I:%M %p"))
        
    def resetScreens(self):
        self.stopCamButtons()
        if self.videoLabelThread is not None:
            self.videoLabelThread.isRunning = False
        
    def openHomeScreen(self):
        self.activeScreen = "home"
        self.resetScreens()
        self.frmHome.raise_()
        
    def openSettingsScreen(self):
        self.activeScreen = "settings"
        self.resetScreens()
        
        # Get Settings.
        if self.parent is not None:
            pass
        
        while self.vlytSettings.count():
            item = self.vlytSettings.takeAt(0)
            widget = item.widget()
        
            widget.deleteLater()
        
        self.refreshDevices()
        for item in self.devices:
            devItem = DeviceItem()
            devItem.container.setText(item["url"])
            devItem.groupName.setText(item["groupName"] if item["groupName"] is not None else "")
            devItem.device.setText(item["type"])
            devItem.id.setText(item["id"])
            devItem.version.setText(item["version"] if item["version"] is not None else "")
            devItem.accepts.setText(", ".join(item["accepts"]) if item["accepts"] is not None and isinstance(item["accepts"], list) else "")
            devItem.vlytSwitch.setAlignment(Qt.AlignCenter)
            if "start" in item["accepts"] and "stop" in item["accepts"]:
                s1 = Switch(thumb_radius=24, track_radius=26)
                s1.clicked.connect(self.doAction)
                if item["active"] is not None and item["active"]:
                    s1.setChecked(True)
                
                devItem.vlytSwitch.addWidget(s1)
            elif "stop" in item["accepts"]:
                s1 = QPushButton()
                s1.setText("✕")
                s1.setStyleSheet(
                    "".join([
                        "QPushButton { background-color: rgb(48, 140, 198);font-size: 26pt; border-radius: 6px;color: white; }\n" 
                        "QPushButton:hover { background-color: rgb(90, 165, 213); }\nQPushButton:pressed { background-color: rgb(150,150,150); }"
                    ])
                )
                s1.setSizePolicy(QtWidgets.QSizePolicy.Fixed, QtWidgets.QSizePolicy.Fixed)
                s1.setMinimumSize(60, 50)
                s1.clicked.connect(self.doAction)
                devItem.vlytSwitch.addWidget(s1)
                
            self.vlytSettings.addWidget(devItem)
        
        self.frmSettings.raise_()
    
    def doAction(self):
        mySwitch = self.sender()
        doStart = False
        if isinstance(mySwitch, Switch) and mySwitch.isChecked():
            doStart = True 
            
        headers = None
        if self.parent.authenticationKey is not None:
            headers = { "Cookie": "token=" + self.parent.authenticationKey }
            
        mydevice = self.sender().parent().parentWidget().parent().id.text()
        for item in self.devices:
            if item["id"] == mydevice:
                myAction = "stop"
                if doStart:
                    myAction = "start"
                
                if item["id"] == "brain":
                    myAction = "shutdown"
                    
                url = urllib.parse.urljoin(self.parent.brain_url, "/device/" + str(item["id"]) + "/" + myAction)
                retVal, retType, retData = sendHTTPRequest(url, type="GET", headers=headers)
                
        self.openSettingsScreen()
            
    def openCamsScreen(self):
        self.activeScreen = "cams"
        self.resetScreens()
        if self.videoLabelThread is not None:
            self.videoLabelThread.isRunning = False
            
        self.refreshDevices()
        self.glytCams.setAlignment(Qt.AlignCenter)
        x = 0
        y = 0
        
        for item in self.devices:
            if item["active"] and "snapshot" in item["accepts"] and "stream" in item["accepts"]:
                btn = VideoButton()
                btn.setBackgroundUrl(
                    urllib.parse.urljoin(self.parent.brain_url, "device/" + str(item["id"]) + "/snapshot"), 
                    authToken=self.parent.authenticationKey
                )
                btn.setSizePolicy(QtWidgets.QSizePolicy(QtWidgets.QSizePolicy.Fixed, QtWidgets.QSizePolicy.Fixed))
                btn.setStyleSheet("color: rgb(255,255,255);")
                btn.setFixedHeight(220)
                btn.setFixedWidth(293)
                btn.clicked.connect(self.openCamViewer)
                btn.start()
                self.glytCams.addWidget(btn, y, x, 1, 1)
                
                x += 1
                
                if x >= 3:
                    y += 1
                    x = 0
                    
        while x != 0 and x < 3:
            btn = QtWidgets.QWidget()
            btn.setSizePolicy(QtWidgets.QSizePolicy(QtWidgets.QSizePolicy.Fixed, QtWidgets.QSizePolicy.Fixed))
            btn.setFixedHeight(220)
            btn.setFixedWidth(293)
            self.glytCams.addWidget(btn, y, x, 1, 1)
            x += 1
            
        while y < 2:
            btn = QtWidgets.QWidget()
            btn.setSizePolicy(QtWidgets.QSizePolicy(QtWidgets.QSizePolicy.Fixed, QtWidgets.QSizePolicy.Fixed))
            btn.setFixedHeight(220)
            btn.setFixedWidth(293)
            self.glytCams.addWidget(btn, y, 0, 1, 1)
            self.glytCams.addWidget(btn, y, 1, 1, 1)
            self.glytCams.addWidget(btn, y, 2, 1, 1)
            
            y += 1

        self.frmCams.raise_()
        
    def openCamViewer(self):

        button = self.sender()
        idx = self.glytCams.indexOf(button)
        location = self.glytCams.getItemPosition(idx)

        self.activeScreen = "cams-viewer"
        self.resetScreens()

        col = location[:2][1]
        row = location[:2][0]
        
        x = 0
        y = 0
        
        fileName = None
        for item in self.devices:
            if item["active"] and "snapshot" in item["accepts"] and "stream" in item["accepts"]:
                if x == col and y == row:
                    fileName = urllib.parse.urljoin(self.parent.brain_url, "device/" + str(item["id"]) + "/stream")
                    break
                
                x += 1
                
                if x >= 3:
                    y += 1
                    x = 0

        if fileName is not None:
            self.videoLabelThread = DownloadThread(fileName, authToken=self.parent.authenticationKey)
            self.videoLabelThread.data_downloaded.connect(self.updateVideoLabel)
            self.lblVideo.setAlignment(Qt.AlignCenter)
            self.videoLabelThread.start()
        
        self.frmCamsViewer.raise_()
    
    def stopCamButtons(self):
        x = 0
        y = 0
        
        for item in self.devices:
            if item["active"] and "snapshot" in item["accepts"] and "stream" in item["accepts"]:
                if self.glytCams.itemAtPosition(y, x):
                    self.glytCams.itemAtPosition(y, x).widget().stop()
                    self.glytCams.itemAtPosition(y, x).widget().deleteLater()

                x += 1
                
                if x >= 3:
                    y += 1
                    x = 0

        while self.glytCams.count():
            item = self.glytCams.takeAt(0)
            widget = item.widget()
        
            widget.deleteLater()
            
    def updateVideoLabel(self, data):
        pixmap = QPixmap()
        pixmap.loadFromData(data)
        self.lblVideo.setPixmap(pixmap)
        return True
            
    def refreshDevices(self):
        headers = None
        if self.parent.authenticationKey is not None:
            headers = { "Cookie": "token=" + self.parent.authenticationKey }
            
        retVal, retType, retData = sendHTTPRequest(urllib.parse.urljoin(self.parent.brain_url, "brain/status"), type="GET", headers=headers)
        
        if retType == "application/json":
            devs = []
            if isinstance(retData, dict):
                for url in retData:
                    for devId in retData[url]:
                        item = retData[url][devId]
                        item["url"] = url
                        devs.append(item)
            
            self.devices = devs
        return True

    def monitorStatuses(self):
        if self.activeScreen == "settings":
            self.openSettingsScreen()


def start():
    app = QApplication(sys.argv)
    form = PanelApp()
    form.show()
    app.exec_()
