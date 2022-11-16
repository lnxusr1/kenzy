from PyQt5 import QtCore, QtGui, QtWidgets
from PyQt5.QtWidgets import QSizePolicy, QAbstractButton
from PyQt5.QtCore import Qt, QSize, QPropertyAnimation, pyqtProperty, QRectF
from PyQt5.QtGui import QPainter

from cgi import parse_header
from kenzy.shared import sendHTTPRequest
import time


class DownloadThread(QtCore.QThread):

    data_downloaded = QtCore.pyqtSignal(object)

    def __init__(self, url, refreshInterval=3, authToken=None):
        QtCore.QThread.__init__(self)
        self.isRunning = False
        self.url = url
        self.refreshInterval = refreshInterval
        self.authToken = authToken 
        
    def run(self):
        self.isRunning = True
        
        headers = None
        if self.authToken is not None:
            headers = { "Cookie": "token=" + self.authToken }
        
        while self.isRunning:
            retVal, retType, retData = sendHTTPRequest(self.url, type="GET", headers=headers)
            if retType.startswith("image/"):
                self.data_downloaded.emit(retData)
                time.sleep(self.refreshInterval)
            elif retType.startswith("multipart/x-mixed-replace"):
                cType, cParam = parse_header(retData.headers.get("content-type"))
                boundary = "--" + str(cParam["boundary"])
                boundary = boundary.encode()
                endHeaders = "\n\n".encode()
                
                part = None
                try:
                    for data in retData.iter_content(chunk_size=64):
                        if not self.isRunning:
                            return 
                        
                        p = data.find(boundary)
                        if p >= 0:
                            
                            if part is None:
                                part = data[:p]
                            else:
                                part += data[:p]
                            
                            iEnd = part.find(endHeaders)
                            if iEnd >= 0:
                                part = part[iEnd + len(endHeaders):]
                                self.data_downloaded.emit(part)
                                
                            part = data[p:]
                        else:
                            if part is None:
                                part = data
                            else:
                                part += data
                except Exception:
                    pass


class VideoButton(QtWidgets.QPushButton):
    def __init__(self, *args, **kwargs):
        self.url = None
        self.refreshInterval = 3
        self.thread = None
        self.pixmap = None
        self.authToken = None
        super().__init__(*args, **kwargs)
        
    def setBackgroundUrl(self, url, refreshInterval=3, authToken=None):
        self.url = url
        self.refreshInterval = refreshInterval
        self.authToken = authToken 
        
        return True
    
    def start(self):
        if self.thread is None or not self.thread.isRunning:
            self.thread = DownloadThread(self.url, refreshInterval=self.refreshInterval, authToken=self.authToken)
            self.thread.data_downloaded.connect(self.setBackgroundImage)
            self.thread.start()
        elif self.thread is not None:
            self.thread.isRunning = True
            
        return True
    
    def stop(self):
        if self.thread is not None:
            self.thread.isRunning = False
            
    def paintEvent(self, event):
        if self.pixmap is not None:
            painter = QPainter(self)
            pixmap = self.pixmap.copy()
            painter.drawPixmap(0, 0, self.width(), self.height(), pixmap)
            
        else:
            super().paintEvent(event)
            
    def setBackgroundImage(self, data):
        pixmap = QtGui.QPixmap()
        pixmap.loadFromData(data)
        pixmap = pixmap.scaled(self.width(), self.height(), QtCore.Qt.KeepAspectRatio)
        
        self.pixmap = pixmap
        self.update()
        return True

    
class VideoLabel(QtWidgets.QLabel):
    def __init__(self, *args, **kwargs):
        self.url = None
        self.refreshInterval = 3
        self.thread = None
        self.authToken = None
        super().__init__(*args, *kwargs)
        
    def setBackgroundUrl(self, url, refreshInterval=3, authToken=None):
        self.url = url
        self.refreshInterval = refreshInterval
        self.authToken = authToken 
        return True
    
    def start(self):
        if self.thread is None or not self.thread.isRunning:
            self.thread = DownloadThread(self.url, refreshInterval=self.refreshInterval, authToken=self.authToken)
            self.thread.data_downloaded.connect(self.setBackgroundImage)
            self.thread.start()
        elif self.thread is not None:
            self.thread.isRunning = True
            
        return True
    
    def stop(self):
        if self.thread is not None:
            self.thread.isRunning = False
            
    def setBackgroundImage(self, data):
        pixmap = QtGui.QPixmap()
        pixmap.loadFromData(data)
        pixmap = pixmap.scaled(self.width(), self.height(), QtCore.Qt.KeepAspectRatio)
        self.setPixmap(pixmap)
        return True

    
class Switch(QAbstractButton):
    def __init__(self, parent=None, track_radius=10, thumb_radius=8):
        super().__init__(parent=parent)
        self.setCheckable(True)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

        self._track_radius = track_radius
        self._thumb_radius = thumb_radius

        self._margin = max(0, self._thumb_radius - self._track_radius)
        self._base_offset = max(self._thumb_radius, self._track_radius)
        self._end_offset = {
            True: lambda: self.width() - self._base_offset,
            False: lambda: self._base_offset,
        }
        self._offset = self._base_offset

        palette = self.palette()
        palette.setColor(QtGui.QPalette.Highlight, QtGui.QColor(48, 148, 198))
        palette.setColor(QtGui.QPalette.HighlightedText, Qt.white)
        palette.setColor(QtGui.QPalette.Light, Qt.white)
        palette.setColor(QtGui.QPalette.Dark, QtGui.QColor(159, 159, 159))
        
        if self._thumb_radius > self._track_radius:
            self._track_color = {
                True: palette.highlight(),
                False: palette.dark(),
            }
            self._thumb_color = {
                True: palette.highlight(),
                False: palette.light(),
            }
            self._text_color = {
                True: palette.highlightedText().color(),
                False: palette.dark().color(),
            }
            self._thumb_text = {
                True: '',
                False: '',
            }
            self._track_opacity = 0.5
        else:
            self._thumb_color = {
                True: palette.highlightedText(),
                False: palette.light(),
            }
            self._track_color = {
                True: palette.highlight(),
                False: palette.dark(),
            }
            self._text_color = {
                True: palette.highlight().color(),
                False: palette.dark().color(),
            }
            self._thumb_text = {
                True: '✔',
                False: '✕',
            }
            self._track_opacity = 1

    @pyqtProperty(int)
    def offset(self):
        return self._offset

    @offset.setter
    def offset(self, value):
        self._offset = value
        self.update()

    def sizeHint(self):  # pylint: disable=invalid-name
        return QSize(
            4 * self._track_radius + 2 * self._margin,
            2 * self._track_radius + 2 * self._margin,
        )

    def setChecked(self, checked):
        super().setChecked(checked)
        self.offset = self._end_offset[checked]()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.offset = self._end_offset[self.isChecked()]()

    def paintEvent(self, event):  # pylint: disable=invalid-name, unused-argument
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(Qt.NoPen)
        track_opacity = self._track_opacity
        thumb_opacity = 1.0
        text_opacity = 1.0
        if self.isEnabled():
            track_brush = self._track_color[self.isChecked()]
            thumb_brush = self._thumb_color[self.isChecked()]
            text_color = self._text_color[self.isChecked()]
        else:
            track_opacity *= 0.8
            track_brush = self.palette().shadow()
            thumb_brush = self.palette().mid()
            text_color = self.palette().shadow().color()

        p.setBrush(track_brush)
        p.setOpacity(track_opacity)
        p.drawRoundedRect(
            self._margin,
            self._margin,
            self.width() - 2 * self._margin,
            self.height() - 2 * self._margin,
            self._track_radius,
            self._track_radius,
        )
        p.setBrush(thumb_brush)
        p.setOpacity(thumb_opacity)
        p.drawEllipse(
            self.offset - self._thumb_radius,
            self._base_offset - self._thumb_radius,
            2 * self._thumb_radius,
            2 * self._thumb_radius,
        )
        p.setPen(text_color)
        p.setOpacity(text_opacity)
        font = p.font()
        font.setPixelSize(1.5 * self._thumb_radius)
        p.setFont(font)
        p.drawText(
            QRectF(
                self.offset - self._thumb_radius,
                self._base_offset - self._thumb_radius,
                2 * self._thumb_radius,
                2 * self._thumb_radius,
            ),
            Qt.AlignCenter,
            self._thumb_text[self.isChecked()],
        )

    def mouseReleaseEvent(self, event):  # pylint: disable=invalid-name
        super().mouseReleaseEvent(event)
        if event.button() == Qt.LeftButton:
            anim = QPropertyAnimation(self, b'offset', self)
            anim.setDuration(120)
            anim.setStartValue(self.offset)
            anim.setEndValue(self._end_offset[self.isChecked()]())
            anim.start()

    def enterEvent(self, event):  # pylint: disable=invalid-name
        self.setCursor(Qt.PointingHandCursor)
        super().enterEvent(event)
        
