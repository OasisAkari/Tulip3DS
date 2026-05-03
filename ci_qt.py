#!/usr/bin/env python3
import os
import shutil
import sys
import traceback
import pyzipper as zipfile
import tempfile
from datetime import datetime
from io import BytesIO
from os import environ
from os.path import abspath, basename, dirname, join, isfile, isdir
from pathlib import Path
from threading import Thread, Lock
from threading import Timer
from time import sleep, time
from typing import Tuple, List, Dict, Optional

try:
    import py7zr
except ImportError:
    py7zr = None

try:
    import rarfile
except ImportError:
    rarfile = None

from PySide6.QtCore import Qt, Signal as pyqtSignal, QObject, QSize, QUrl
from PySide6.QtGui import QPixmap, QIcon, QDragEnterEvent
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
                             QLabel, QLineEdit, QPushButton, QFileDialog, QTreeWidget,
                             QTreeWidgetItem, QProgressBar, QCheckBox, QMessageBox,
                             QTextEdit, QSplitter, QDialog, QAbstractItemView, QSystemTrayIcon,
                             QHeaderView)
from pyctr.crypto import MissingSeedError, CryptoEngine, load_seeddb
from pyctr.crypto.engine import b9_paths, BootromNotFoundError
from pyctr.type.cdn import CDNError, CDNReader
from pyctr.type.cia import CIAError, CIAReader
from pyctr.type.tmd import TitleMetadataError
from pyctr.util import config_dirs

from conv_embed import conventer

from custominstall import CustomInstall, load_cifinish, InvalidCIFinishError, InstallStatus, is_windows, get_install_size

# from winmica import is_mica_supported, ApplyMica, MicaType

# This file is a part of custom-install.py.
#
# custom-install is copyright (c) 2019-2020 Ian Burgwin
# This file is licensed under The MIT License (MIT).
# You can find the full license text in LICENSE.md in the root of this project.

file_parent = dirname(abspath(__file__))

CI_VERSION = 'OasisAkari Modded 1.5'


# automatically load boot9 if it's in the current directory
b9_paths.insert(0, join(file_parent, 'boot9.bin'))
b9_paths.insert(0, join(file_parent, 'boot9_prot.bin'))

seeddb_paths = [join(x, 'seeddb.bin') for x in config_dirs]
try:
    seeddb_paths.insert(0, environ['SEEDDB_PATH'])
except KeyError:
    pass
# automatically load seeddb if it's in the current directory
seeddb_paths.insert(0, join(file_parent, 'seeddb.bin'))


taskbar = None
if is_windows:
    try:
        import comtypes.client as cc

        tbl = cc.GetModule(file_parent + '/TaskbarLib.tlb')

        taskbar = cc.CreateObject('{56FDF344-FD6D-11D0-958A-006097C9A090}', interface=tbl.ITaskbarList3)
        taskbar.HrInit()
    except (ModuleNotFoundError, UnicodeEncodeError, AttributeError):
        traceback.print_exc()
        pass

def find_first_file(paths):
    for p in paths:
        if isfile(p):
            return p.replace('\\', '/')


timer: Dict[str, Timer] = {}

def debounce(func, delay):
    def wrapper(*args, **kwargs):
        if (t:=timer.get(func.__name__, None)) is not None:
            t.cancel()
            del timer[func.__name__]
        # 设置新的计时器
        timer[func.__name__] = Timer(delay, func, args=args, kwargs=kwargs)
        timer[func.__name__].start()
    return wrapper


# find boot9, seeddb, and movable.sed to auto-select in the gui
default_b9_path = find_first_file(b9_paths)
default_seeddb_path = find_first_file(seeddb_paths)
default_movable_sed_path = find_first_file([join(file_parent, 'movable.sed')])

if default_seeddb_path:
    load_seeddb(default_seeddb_path)

statuses = {
    InstallStatus.Waiting: '等待中',
    InstallStatus.Starting: '安装中',
    InstallStatus.Writing: '写入中',
    InstallStatus.Finishing: '完成中',
    InstallStatus.Done: '完成',
    InstallStatus.Failed: '失败',
}


def format_file_size(size: int) -> str:
    if size < 1024:
        return f'{size} B'

    units = ['KiB', 'MiB', 'GiB', 'TiB']
    value = float(size)
    for unit in units:
        value /= 1024
        if value < 1024 or unit == units[-1]:
            return f'{value:.1f} {unit}'

    return f'{size} B'


def get_disk_info(path: str, log=print) -> Tuple[str, str]:
    try:
        if not path or not isdir(path):
            return '', ''
        usage = shutil.disk_usage(path)
        return format_file_size(usage.total), format_file_size(usage.free)
    except Exception as e:
        log(f'获取磁盘信息失败: {e}')
        return '', ''


class ConvertDialog(QDialog):
    """Dialog for converting 3DS/CCI files to CIA format"""
    # Signals to safely communicate between conversion thread and GUI thread
    convert_progress_signal = pyqtSignal(float, int, int, int, int)
    status_signal = pyqtSignal(str)
    error_signal = pyqtSignal(str, str)
    info_signal = pyqtSignal(str, str)
    finished_signal = pyqtSignal()
    def __init__(self, parent, boot9_path: str, log_func=print):
        super().__init__(parent)
        self.setWindowTitle("转换 3DS/CCI 文件为 CIA")
        self.setAcceptDrops(True)

        self.boot9_path = boot9_path
        self.log = log_func
        self.files_to_convert = []
        self.is_converting = False

        # Setup layout
        layout = QVBoxLayout(self)

        # Top: Select file button
        button_layout = QHBoxLayout()
        select_button = QPushButton("选择文件")
        select_button.clicked.connect(self.select_files)
        button_layout.addWidget(select_button)
        button_layout.addStretch()
        layout.addLayout(button_layout)

        # Center: Drag and drop area with text
        self.drop_area = QLabel("拖拽文件到此处转换为 CIA")
        self.drop_area.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.drop_area.setStyleSheet(
            "border: 2px dashed #ccc; "
            "border-radius: 5px; "
            "padding: 50px; "
            "color: #666;"
        )
        self.drop_area.setMinimumHeight(200)
        layout.addWidget(self.drop_area)
        # Bottom: Progress bar
        self.progress_bar = QProgressBar()
        self.progress_bar.setValue(0)
        layout.addWidget(self.progress_bar)

        # Progress text
        self.progress_text = QLabel("")
        layout.addWidget(self.progress_text)

        # Bottom buttons: only close button is needed; conversion starts automatically
        button_layout_bottom = QHBoxLayout()
        button_layout_bottom.addStretch()
        close_button = QPushButton("关闭")
        close_button.clicked.connect(self.close)
        button_layout_bottom.addWidget(close_button)
        layout.addLayout(button_layout_bottom)

        # Connect signals to slots so background threads can update GUI safely
        self.convert_progress_signal.connect(self._update_progress)
        self.status_signal.connect(self._set_status_text)
        self.error_signal.connect(self._show_error_message)
        self.info_signal.connect(self._show_info_message)
        self.finished_signal.connect(self._on_finished)

    def select_files(self):
        """Open file dialog to select 3DS/CCI files"""
        file_filter = "游戏卡镜像 (*.3ds *.cci);;所有文件 (*)"
        file_paths, _ = QFileDialog.getOpenFileNames(
            self,
            "选择 3DS/CCI 文件",
            "",
            file_filter
        )
        if file_paths:
            self.files_to_convert = file_paths
            # Start conversion immediately after selection
            self.start_conversion()

    def dragEnterEvent(self, e: QDragEnterEvent):
        """Handle drag enter event"""
        if e.mimeData().hasUrls():
            e.accept()
            self.drop_area.setStyleSheet(
                "border: 2px dashed #0078d4; "
                "border-radius: 5px; "
                "padding: 50px; "
                "color: #0078d4;"
            )
        else:
            e.ignore()

    def dragLeaveEvent(self, e):
        """Handle drag leave event"""
        self.drop_area.setStyleSheet(
            "border: 2px dashed #ccc; "
            "border-radius: 5px; "
            "padding: 50px; "
            "color: #666;"
        )

    def dropEvent(self, e):
        """Handle drop event"""
        self.drop_area.setStyleSheet(
            "border: 2px dashed #ccc; "
            "border-radius: 5px; "
            "padding: 50px; "
            "color: #666;"
        )

        urls = e.mimeData().urls()
        new_files = []
        for url in urls:
            path = url.toLocalFile()
            if isfile(path) and path.lower().endswith(('.3ds', '.cci')):
                new_files.append(path)

        if new_files:
            self.files_to_convert.extend(new_files)
            # Start conversion immediately after drop
            self.start_conversion()

    def _prepare_conversion_jobs(self):
        """Pre-check files before starting conversion.

        Returns a list of (file_path, overwrite) tuples for files that can be converted.
        """
        conversion_jobs = []
        skipped_files = 0

        for file_path in list(self.files_to_convert):
            output_dir = dirname(file_path)
            if not output_dir or not isdir(output_dir):
                self.log(f'跳过无法访问的输出目录：{output_dir}')
                skipped_files += 1
                continue

            cia_name = join(output_dir, Path(file_path).stem + '.cia')
            overwrite = False

            if isfile(cia_name):
                confirm = QMessageBox.question(
                    self,
                    "确认覆盖",
                    f"目标目录中已存在同名 CIA 文件：\n{cia_name}\n\n是否覆盖？",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
                )
                if confirm != QMessageBox.StandardButton.Yes:
                    self.log(f'已跳过同名 CIA：{cia_name}')
                    skipped_files += 1
                    continue
                overwrite = True

            try:
                required_size = os.path.getsize(file_path)
                free_space = shutil.disk_usage(output_dir).free
            except Exception as e:
                self.log(f'检查空间失败，跳过 {basename(file_path)}：{e}')
                skipped_files += 1
                continue

            if required_size > free_space:
                QMessageBox.warning(
                    self,
                    "空间不足",
                    f"目标目录空间不足，已拒绝转换：\n{file_path}\n\n"
                    f"所需空间: {format_file_size(required_size)}\n"
                    f"可用空间: {format_file_size(free_space)}"
                )
                self.log(f'空间不足，已拒绝转换：{file_path}')
                skipped_files += 1
                continue

            conversion_jobs.append((file_path, overwrite))

        if skipped_files and not conversion_jobs:
            QMessageBox.information(self, "提示", "没有可转换的文件。")
        self.files_to_convert.clear()
        return conversion_jobs

    def start_conversion(self):
        """Start converting files"""
        if not self.files_to_convert or self.is_converting:
            return

        if not self.boot9_path or not isfile(self.boot9_path):
            # Use signal to show warning on main thread
            self.error_signal.emit("错误", "boot9.bin 文件路径无效")
            return

        conversion_jobs = self._prepare_conversion_jobs()
        if not conversion_jobs:
            self.is_converting = False
            self.progress_bar.setValue(0)
            self.progress_text.setText("没有可转换的文件")
            return

        self.is_converting = True
        # prepare UI
        self.progress_bar.setValue(0)
        self.progress_text.setText("")

        def conversion_task():
            total_files = len(conversion_jobs)
            for idx, (file_path, overwrite) in enumerate(conversion_jobs, 1):
                try:
                    # Get the directory of the file for in-place conversion
                    output_dir = dirname(file_path)

                    # Inform main thread about status
                    self.log(f'正在转换文件 ({idx}/{total_files}): {basename(file_path)}')
                    self.status_signal.emit(f"正在转换: {basename(file_path)} ({idx}/{total_files})")

                    # Call converter function
                    conventer(
                        log=self.log,
                        verbose=True,
                        game=[file_path],
                        output=output_dir,
                        overwrite=overwrite,
                        boot9=self.boot9_path,
                        ignore_bad_hashes=False,
                        on_progress=lambda percent, read, size: self.convert_progress_signal.emit(percent, read, size, idx, total_files)
                    )

                    self.log(f'转换完成: {basename(file_path)}')

                except Exception as e:
                    self.log(f'转换失败 {basename(file_path)}: {e}')
                    # Show error in main thread
                    self.error_signal.emit("转换错误", f"转换 {basename(file_path)} 时出错:\n{str(e)}")

            # Notify main thread that conversion finished
            self.info_signal.emit("完成", "已转换完成，请检查目录")
            self.finished_signal.emit()
            self.log("所有文件转换完成")

        # Run conversion in a separate thread
        conversion_thread = Thread(target=conversion_task, daemon=True)
        conversion_thread.start()

    def _update_progress(self, percent, read, size, current_file, total_files):
        """Update progress bar (called from conversion thread)"""
        # Calculate overall progress based on files
        overall_percent = int((current_file - 1 + percent / 100) / total_files * 100)
        self.progress_bar.setValue(overall_percent)

    def _set_status_text(self, text: str):
        """Set status text on main thread"""
        self.progress_text.setText(text)

    def _show_error_message(self, title: str, message: str):
        """Show an error message box on main thread"""
        QMessageBox.critical(self, title, message)

    def _show_info_message(self, title: str, message: str):
        """Show an information message box on main thread"""
        QMessageBox.information(self, title, message)

    def _on_finished(self):
        """Finalize UI when conversion finishes (executed on main thread)"""
        self.is_converting = False
        self.progress_bar.setValue(100)
        self.progress_text.setText("转换完成")


class InstallSignals(QObject):
    log_signal = pyqtSignal(str)
    progress_signal = pyqtSignal(float, int, int)
    convert_progress_signal = pyqtSignal(float, int, int)
    error_signal = pyqtSignal(Exception)
    cia_start_signal = pyqtSignal(int)
    status_signal = pyqtSignal(str, InstallStatus)
    installed_signal = pyqtSignal(list, bool, int)
    failed_signal = pyqtSignal(list)
    remove_signal = pyqtSignal()
    force_install_signal = pyqtSignal(bool)
    export_finalize_signal = pyqtSignal()
    recover_pending_install_signal = pyqtSignal()
    delete_corrupted_files_signal = pyqtSignal()
    finished_signal = pyqtSignal()


signals = InstallSignals()


class AboutDialog(QDialog):
    def __init__(self, parent: 'CustomInstallGUI'):
        super().__init__(parent)
        self.setWindowTitle("关于 custom-install")
        self.setMinimumSize(QSize(300, 300))

        # Setup layout
        layout = QVBoxLayout(self)

        # Add text
        about_text = (
            f"<h2>custom-install {CI_VERSION}</h2>"
            "<p>汉化 & 界面重构 By OasisAkari （一只火狐） - <a href='https://stray-soul.com/'>https://stray-soul.com/</a></p>"
            "<p>禁止二次出售（如闲鱼等平台）与商用。</p>"
            "<p>原程序作者：ihaveamac - <a href='https://github.com/ihaveamac/custom-install'>https://github.com/ihaveamac/custom-install</a></p>"
            "<p>本 GUI 参考了 chinnsenn 的实现： <a href='https://github.com/chinnsenn/custom-install/tree/safe-install'>https://github.com/chinnsenn/custom-install/tree/safe-install</a></p>"
            "<p>在此表示感谢。</p>"
            "<p>如果你在使用过程中遇到了问题，请先检查一下使用教程：<a href='https://stray-soul.com/ci.html'>https://stray-soul.com/ci.html</a>"
            "<p>本修改版开源地址：<a href='https://github.com/OasisAkari/custom-install/tree/qt-hans'>https://github.com/OasisAkari/custom-install/tree/qt-hans</a></p>"
            "<p>生活不易，如果您觉得工具好用可以点击这里支持我：<a href='https://stray-soul.com/donate.html'>https://stray-soul.com/donate.html</a></p>"
        )

        about_label = QLabel(about_text)
        about_label.setOpenExternalLinks(True)
        layout.addWidget(about_label)

        # Add force install checkbox
        self.force_install_checkbox = QCheckBox("强制安装（跳过哈希检查）")
        self.force_install_checkbox.setToolTip("如果你知道自己在做什么，可以启用此选项。")
        layout.addWidget(self.force_install_checkbox)

        # Add a note about the force install checkbox

        def force_install_changed_warning():
            if self.force_install_checkbox.isChecked():
                w = QMessageBox.critical(self, "警告",
                                    "启用强制安装将会尝试安装损坏的应用，安装后的应用很有可能会中途崩溃或无法使用。\n"
                                    "若你遇到了此类问题，首先应该做的是尝试重新下载资源，而不是启用此选项。\n"
                                    "除非你知道你自己在做什么，否则请不要启用此选项！\n"
                                    "本工具作者对安装损坏的应用产生的后果概不负责。",
                                    QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel,
                                    )
                if w == QMessageBox.StandardButton.Cancel:
                    self.force_install_checkbox.setChecked(False)
            signals.force_install_signal.emit(self.force_install_checkbox.isChecked())


        self.force_install_checkbox.clicked.connect(force_install_changed_warning)

        # add export custom-install-finalize button
        export_button = QPushButton("导出 custom-install-finalize")
        layout.addWidget(export_button)

        def export_finalize():
            confirm = QMessageBox.question(
                self, "导出 custom-install-finalize", "你确定要导出 custom-install-finalize 吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if confirm == QMessageBox.StandardButton.Yes:
                signals.export_finalize_signal.emit()

        export_button.clicked.connect(export_finalize)

        recover_button = QPushButton("恢复未完成的安装")
        layout.addWidget(recover_button)

        def recover_pending_install():
            confirm = QMessageBox.question(
                self, "恢复未完成的安装", "你确定要恢复未完成的安装吗？\n"
                "这将会尝试从 SD 卡根目录的 ci-pending 文件夹中恢复上次未完成的安装。",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if confirm == QMessageBox.StandardButton.Yes:
                signals.recover_pending_install_signal.emit()
        recover_button.clicked.connect(recover_pending_install)

        # Add delete corrupted files button
        delete_button = QPushButton("删除损坏的文件")
        layout.addWidget(delete_button)
        def delete_corrupted_files():
            confirm = QMessageBox.question(
                self, "删除损坏的文件", "你确定要删除损坏的文件吗？\n"
                "这将会尝试从 SD 卡根目录的 ci-install-temp 为前缀的文件夹中删除所有损坏的文件。",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if confirm == QMessageBox.StandardButton.Yes:
                signals.delete_corrupted_files_signal.emit()

        delete_button.clicked.connect(delete_corrupted_files)

        convert_button = QPushButton("转换 3DS / CCI 为 CIA 格式")

        def open_convert_dialog():
            d = ConvertDialog(self, parent.boot9_path.text(), parent.log)
            d.show()

        layout.addWidget(convert_button)
        convert_button.clicked.connect(open_convert_dialog)


        # Add close button
        close_button = QPushButton("关闭")
        layout.addWidget(close_button)
        close_button.clicked.connect(self.close)

                # if is_mica_supported():
                #     self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
                #     hwnd = int(self.winId())
                #     ApplyMica(hwnd, MicaType.MICA)




class ListBoxDialog(QDialog):
    def __init__(self, parent, title: str, desc: str, items: List[str]):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumSize(QSize(200, 200))

        # Setup layout
        layout = QVBoxLayout(self)

        # Add description
        desc_label = QLabel(desc)
        desc_label.setWordWrap(True)
        layout.addWidget(desc_label)

        # Create list widget
        self.list_widget = QTreeWidget()
        self.list_widget.setHeaderHidden(True)
        for item in items:
            self.list_widget.addTopLevelItem(QTreeWidgetItem([item]))

        # Add list widget to layout
        layout.addWidget(self.list_widget)

        # Add close button
        close_button = QPushButton("关闭")
        close_button.clicked.connect(self.close)
        layout.addWidget(close_button)

        # if is_mica_supported():
        #     self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        #     hwnd = int(self.winId())
        #     ApplyMica(hwnd, MicaType.MICA)


class ScrollableErrorDialog(QDialog):
    def __init__(self, parent, title: str, message: str):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumSize(QSize(500, 300))

        # Setup layout
        layout = QVBoxLayout(self)

        # Create scrollable text edit
        self.text_edit = QTextEdit()
        self.text_edit.setText(message)
        self.text_edit.setReadOnly(True)
        layout.addWidget(self.text_edit)

        # Add close button
        close_button = QPushButton("关闭")
        close_button.clicked.connect(self.close)
        layout.addWidget(close_button)

        # if is_mica_supported():
        #     self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        #     hwnd = int(self.winId())
        #     ApplyMica(hwnd, MicaType.MICA)


class PasswordInputDialog(QDialog):
    """Dialog for inputting password for encrypted archives"""
    def __init__(self, parent, archive_name: str):
        super().__init__(parent)
        self.setWindowTitle("输入压缩包密码")
        self.setMinimumWidth(400)
        self.password = None

        layout = QVBoxLayout(self)
        
        # Prompt label
        prompt_label = QLabel(f"压缩包 '{archive_name}' 需要输入密码：")
        layout.addWidget(prompt_label)
        
        # Password input field
        self.password_input = QLineEdit()
        self.password_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_input.returnPressed.connect(self.accept)
        layout.addWidget(self.password_input)
        
        # Buttons
        button_layout = QHBoxLayout()
        ok_button = QPushButton("确定")
        ok_button.clicked.connect(self.accept)
        cancel_button = QPushButton("取消")
        cancel_button.clicked.connect(self.reject)
        
        button_layout.addStretch()
        button_layout.addWidget(ok_button)
        button_layout.addWidget(cancel_button)
        layout.addLayout(button_layout)
    
    def accept(self):
        self.password = self.password_input.text()
        super().accept()


class CustomInstallGUI(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setAcceptDrops(True)
        self.setWindowTitle(f'custom-install {CI_VERSION}')
        self.resize(800, 600)

        # Setup main widget and layout
        self.central_widget = QWidget()
        self.setCentralWidget(self.central_widget)
        self.layout = QVBoxLayout(self.central_widget)

        # Initialize variables
        self.readers = {}
        self.lock = Lock()
        self.b9_loaded = False
        self.signals = signals

        # Setup UI components
        # SD Root picker
        sd_layout = QHBoxLayout()
        self.sd_label = QLabel('SD 卡根目录：')
        self.sd_path = QLineEdit()
        self.sd_button = QPushButton('选择')
        self.sd_button.clicked.connect(self.select_sd_root)
        sd_layout.addWidget(self.sd_label)
        sd_layout.addWidget(self.sd_path)
        sd_layout.addWidget(self.sd_button)
        self.layout.addLayout(sd_layout)

        # Boot9 picker
        boot9_layout = QHBoxLayout()
        self.boot9_label = QLabel('boot9.bin 文件：')
        self.boot9_path = QLineEdit()
        if default_b9_path:
            self.boot9_path.setText(default_b9_path)
        self.boot9_button = QPushButton('选择')
        self.boot9_button.clicked.connect(lambda: self.select_file('boot9', '*.bin'))
        boot9_layout.addWidget(self.boot9_label)
        boot9_layout.addWidget(self.boot9_path)
        boot9_layout.addWidget(self.boot9_button)
        self.layout.addLayout(boot9_layout)

        # Seeddb picker
        seeddb_layout = QHBoxLayout()
        self.seeddb_label = QLabel('seeddb 文件：')
        self.seeddb_path = QLineEdit()
        if default_seeddb_path:
            self.seeddb_path.setText(default_seeddb_path)
        self.seeddb_button = QPushButton('选择')
        self.seeddb_button.clicked.connect(lambda: self.select_file('seeddb', '*.bin'))
        seeddb_layout.addWidget(self.seeddb_label)
        seeddb_layout.addWidget(self.seeddb_path)
        seeddb_layout.addWidget(self.seeddb_button)
        self.layout.addLayout(seeddb_layout)

        # Movable.sed picker
        movable_layout = QHBoxLayout()
        self.movable_label = QLabel('movable.sed 文件：')
        self.movable_path = QLineEdit()
        if default_movable_sed_path:
            self.movable_path.setText(default_movable_sed_path)
        self.movable_button = QPushButton('选择')
        self.movable_button.clicked.connect(lambda: self.select_file('movable.sed', '*.sed'))
        movable_layout.addWidget(self.movable_label)
        movable_layout.addWidget(self.movable_path)
        movable_layout.addWidget(self.movable_button)
        self.layout.addLayout(movable_layout)


        button_layout = QHBoxLayout()

        self.add_cia_button = QPushButton('添加应用')
        self.add_cia_button.setEnabled(False)
        self.add_cia_button.clicked.connect(self.add_cias)
        button_layout.addWidget(self.add_cia_button)

        self.add_cdn_button = QPushButton('添加 CDN 应用目录')
        self.add_cdn_button.setEnabled(False)
        self.add_cdn_button.clicked.connect(self.add_cdn)
        button_layout.addWidget(self.add_cdn_button)

        self.add_folder_button = QPushButton('添加应用文件夹')
        self.add_folder_button.setEnabled(False)
        self.add_folder_button.clicked.connect(self.add_folder)
        button_layout.addWidget(self.add_folder_button)

        self.remove_button = QPushButton('移除选择的内容（按住CTRL键多选）')
        self.remove_button.setEnabled(False)
        self.remove_button.clicked.connect(self.remove_selected)
        button_layout.addWidget(self.remove_button)

        self.layout.addLayout(button_layout)

        # Create a splitter for the tree view and log window
        self.splitter = QSplitter()
        self.splitter.setOrientation(Qt.Orientation.Vertical)
        self.layout.addWidget(self.splitter)

        # Title list
        self.title_list = QTreeWidget()
        self.title_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.title_list.setIndentation(5)
        self.title_list.setHeaderLabels(['图标', '文件路径', '应用 ID', '应用名', '应用大小', '安装状态'])
        self.title_icon_size = QSize(18, 18)
        self.title_list.setIconSize(self.title_icon_size)

        # Set column resize modes for adaptive width
        self.title_list.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)  # Icon
        self.title_list.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)  # File path
        self.title_list.header().setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)  # App ID
        self.title_list.header().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)  # App name - adaptive
        self.title_list.header().setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)  # App size
        self.title_list.header().setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)  # Install status

        self.title_list.header().setMinimumSectionSize(self.title_icon_size.width() + 8)
        self.title_list.setColumnWidth(0, self.title_icon_size.width() + 15)
        self.title_list.setColumnWidth(1, 200)
        self.title_list.setColumnWidth(2, 150)
        self.title_list.setColumnWidth(4, 70)
        self.title_list.setColumnWidth(5, 10)
        self.splitter.addWidget(self.title_list)

        # Log window
        self.log_window = QTextEdit()
        self.log_window.setReadOnly(True)
        self.log_window.setMinimumHeight(100)
        self.splitter.addWidget(self.log_window)

        # Setup progress bar
        self.progress_bar_text = QLabel('')
        self.layout.addWidget(self.progress_bar_text)
        self.progress_bar = QProgressBar()
        self.progress_bar.setMaximum(100)
        self.layout.addWidget(self.progress_bar)

        if taskbar:
            # Set up taskbar button
            taskbar.ActivateTab(int(self.winId()))

        # Control buttons and options
        control_layout = QHBoxLayout()

        self.skip_contents = QCheckBox('跳过内容（仅将信息加入数据库）')
        control_layout.addWidget(self.skip_contents)

        self.overwrite_saves = QCheckBox('覆盖已有存档')
        control_layout.addWidget(self.overwrite_saves)

        self.install_and_delete = QCheckBox('安装后删除文件')
        control_layout.addWidget(self.install_and_delete)

        self.start_button = QPushButton('开始安装')
        self.start_button.clicked.connect(self.start_install)
        control_layout.addWidget(self.start_button)

        self.about_button = QPushButton("关于")
        self.about_button.clicked.connect(self.show_about)
        control_layout.addWidget(self.about_button)

        self.save_log_button = QPushButton("导出日志")
        self.save_log_button.clicked.connect(self.save_log)
        control_layout.addWidget(self.save_log_button)

        self.dialog = None

        self.layout.addLayout(control_layout)

        # Status and info layout
        status_info_layout = QHBoxLayout()
        self.status_label = QLabel()
        status_info_layout.addWidget(self.status_label, 1)

        self.info_label = QLabel()
        self.info_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        status_info_layout.addWidget(self.info_label, 1)

        self.layout.addLayout(status_info_layout)

        # textChanged signals
        self.sd_path.textChanged.connect(self.update_button_states)
        self.movable_path.textChanged.connect(self.update_button_states)
        self.seeddb_path.textChanged.connect(self.update_button_states)
        self.boot9_path.textChanged.connect(self.update_button_states)

        # Setup signals
        self.signals.log_signal.connect(self.on_log)
        self.signals.progress_signal.connect(self.on_progress)
        self.signals.convert_progress_signal.connect(self.on_convert_progress)
        self.signals.error_signal.connect(self.on_error)
        self.signals.cia_start_signal.connect(self.on_cia_start)
        self.signals.status_signal.connect(self.on_status_update)
        self.signals.installed_signal.connect(self.on_installed_signal)
        self.signals.failed_signal.connect(self.on_failed_signal)
        self.signals.remove_signal.connect(self.on_remove_signal)
        self.signals.force_install_signal.connect(self.on_force_install_signal)
        self.signals.export_finalize_signal.connect(self.on_export_finalize_signal)
        self.signals.recover_pending_install_signal.connect(self.on_recover_pending_install_signal)
        self.signals.delete_corrupted_files_signal.connect(self.on_delete_corrupted_files_signal)
        self.signals.finished_signal.connect(self.on_finished_signal)

        # Initial state
        self.log(f'custom-install {CI_VERSION} - https://github.com/OasisAkari/custom-install')
        self.log(f'汉化 & 修改 By OasisAkari （一只火狐） - https://stray-soul.com/，请勿二次出售（如闲鱼等平台）与商用。')
        self.log("就绪。")

        # if is_mica_supported():
        #     # self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        #     hwnd = int(self.winId())
        #     ApplyMica(hwnd, MicaType.MICA)

        self.force_install = False
        self.skip_game_card_confirm = False
        self.total_items = 0
        self.finished_percent = 0
        self.tray_icon = QSystemTrayIcon(self.windowIcon(), self)
        self.tray_icon.show()

        self.enabled_button = False


    def show_about(self):
        if not self.dialog:
            self.dialog = AboutDialog(self)
        self.dialog.show()
        self.dialog.setFixedSize(self.dialog.size())

    def select_sd_root(self):
        qurl = QUrl.fromLocalFile(str(Path.home()))
        if is_windows:
            qurl = QUrl("clsid:0AC0837C-BBF8-452A-850D-79D08E667CA7")
        directory = QFileDialog.getExistingDirectoryUrl(self, "选择 SD 卡根目录", qurl)
        directory = directory.toLocalFile() if directory else None
        if directory:
            cifinish_path = join(directory, 'cifinish.bin')
            try:
                load_cifinish(cifinish_path)
            except InvalidCIFinishError:
                QMessageBox.critical(self, '错误',
                                     f'卡内的{cifinish_path}是损坏的！\n\n'
                                    f'这可能代表着 SD 卡或其文件系统出错。请使用磁盘检查工具查找错误。\n'
                                    f'这也可能是 custom-install 的问题（虽然不太可能）。\n\n'
                                    f'请停止操作，然后尝试检查一下，以防止出现更大的问题。但如果你想再试一次，请删除 SD 卡根目录的 cifinish.bin，然后重新启动 custom-install。')
                return

            self.sd_path.setText(directory)

            # Auto-detect files
            for filename in ['boot9.bin', 'seeddb.bin', 'movable.sed']:
                path = self.auto_detect_file(directory, filename)
                if filename == 'boot9.bin':
                    self.check_b9_loaded()
                if filename == 'seeddb.bin' and path:
                    load_seeddb(path)
        self.update_button_states()


    def auto_detect_file(self, sd_root: str, filename: str) -> Optional[str]:
        paths = [join(sd_root, 'gm9', 'out', filename), join(sd_root, filename)]
        found_path = find_first_file(paths)
        if found_path:
            self.log(f'从 SD 卡的 {found_path} 找到了 {filename}')
            if filename == 'boot9.bin':
                self.boot9_path.setText(found_path)
            elif filename == 'seeddb.bin':
                self.seeddb_path.setText(found_path)
            elif filename == 'movable.sed':
                self.movable_path.setText(found_path)
            return found_path
        return None

    def select_file(self, file_type: str, file_filter: str):
        file_name, _ = QFileDialog.getOpenFileName(self, f"选择 {file_type}", "", f"{file_type} ({file_filter})")
        if file_name:
            if file_type == 'boot9':
                self.boot9_path.setText(file_name.replace('\\', '/'))
                self.check_b9_loaded()
            elif file_type == 'seeddb':
                self.seeddb_path.setText(file_name.replace('\\', '/'))
                load_seeddb(file_name.replace('\\', '/'))
            elif file_type == 'movable.sed':
                self.movable_path.setText(file_name.replace('\\', '/'))
        self.update_button_states()

    def _add_cias(self, paths):
        if not self.enabled_button:
            QMessageBox.warning(self, "错误", "请先选择 SD 卡根目录及 movable.sed。")
            return
        failed = {}
        for f in paths:
            success, reason = self.add_cia(f)
            if not success:
                failed[f] = reason

        if failed:
            error_text = "无法添加以下文件：\n\n"
            for path, reason in failed.items():
                error_text += f"{basename(path)}: {reason}\n"
            dialog = ScrollableErrorDialog(self, "无法添加应用", error_text)
            dialog.exec()

    def add_cias(self):
        files, _ = QFileDialog.getOpenFileNames(self, "选择应用文件", "", "应用文件 (*.cia *.3ds *.cci *.zip *.7z *.rar)")
        if files:
            self._add_cias(files)

    def add_cdn(self):
        directory = QFileDialog.getExistingDirectory(self, "选择 CDN 应用文件夹")
        if directory:
            if isfile(join(directory, 'tmd')):
                success, reason = self.add_cia(directory)
                if not success:
                    QMessageBox.critical(self, "错误", f"无法添加 {basename(directory)}：{reason}")
            else:
                QMessageBox.critical(self, "错误", f"CDN 文件夹内未找到 tmd 文件：\n{directory}")

    def _add_folder(self, path, delete=False):
        if not self.enabled_button:
            QMessageBox.warning(self, "错误", "请先选择 SD 卡根目录及 movable.sed。")
            return
        if path:
            path = str(path).replace('\\', '/')
            failed = {}
            for root, dirs, files in os.walk(path):
                for file in files:
                    file_path = join(root, file).replace('\\', '/')
                    if file_path.lower().endswith('.cia'):
                        success, reason = self.add_cia(str(file_path))
                        if not success:
                            failed[file_path] = reason
                            if delete:
                                self.log("无法添加文件 " + file_path + "，正在删除...")
                                os.remove(file_path)
                                self.log("已删除：" + file_path)
                                _p = Path(file_path)
                                if _p.parent.exists():
                                    if not any(_p.parent.iterdir()) and _p.parent.name.startswith('ci-install-temp'):
                                        _pp = str(_p.parent).replace("\\", "/")
                                        self.log(f'目录 {_pp} 为空，尝试删除...')
                                        _p.parent.rmdir()
                                        self.log(f'已删除空目录：{_pp}')
                        else:
                            if delete:
                                self.pending_remove.append(str(file_path))
                    if file_path.lower().endswith('.3ds') or file_path.lower().endswith('.cci'):
                        self.add_game_card_image(str(file_path))
                        if delete:
                            self.pending_remove.append(str(file_path))
            if failed:
                error_text = ["无法添加以下文件：\n"]
                for path, reason in failed.items():
                    error_text += [f"{basename(path)}: {reason}\n"]
                dialog = ScrollableErrorDialog(self, "添加软件失败", "\n".join(error_text))
                dialog.exec()

    def add_folder(self):
        directory, _ = QFileDialog.getOpenFileName(self, "选择包含了应用文件的文件夹", "", "应用文件 (*.cia *.3ds *.cci)")
        _dir = str(Path(directory).parent)
        self._add_folder(_dir)

    def remove_selected(self):
        for item in self.title_list.selectedItems():
            self.title_list.takeTopLevelItem(self.title_list.indexOfTopLevelItem(item))
            path = item.text(1)
            if path in self.readers:
                del self.readers[path]
            self.log(f'已从列表中移除：{path}')
            self.log(f"检查待删除列表中是否包含 {path}...")
            self.log(*self.pending_remove)
            if path in self.pending_remove:
                self.pending_remove.remove(path)
                if os.path.exists(path):
                    try:
                        self.log(f'正在删除目录 {path}...')
                        os.remove(path)
                        self.log("已删除：", path)
                        _p = Path(path)
                        if _p.parent.exists():
                            if not any(_p.parent.iterdir()) and _p.parent.name.startswith('ci-install-temp'):
                                _pp = str(_p.parent).replace("\\", "/")
                                self.log(f'目录 {_pp} 为空，尝试删除...')
                                _p.parent.rmdir()
                                self.log(f'已删除空目录：{_pp}')
                    except Exception as e:
                        self.log(f'无法删除目录 {path}：{e}')
            else:
                self.log(f'待删除列表中不包含 {path}，无需执行任何操作。')
        self.update_info_label()

    def add_compressed_file(self, path: str) -> Tuple[bool, str]:
        """
        处理压缩文件（zip、7z、rar等）
        先处理并验证密码，再检查压缩包内是否存在.cia/.3ds/.cci文件
        解压到临时文件夹并传递给_add_folder
        """
        try:
            path = path.replace('\\', '/')
            self.log(f'开始处理压缩文件：{path}')

            confirm = QMessageBox.question(
                self,
                '确认解压',
                f'检测到压缩文件：\n{path}\n\n是否现在解压？',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if confirm != QMessageBox.StandardButton.Yes:
                self.log(f'已跳过压缩文件：{path}')
                return True, ''

            # 确定压缩文件类型
            file_ext = path.lower().split('.')[-1]
            archive = None
            file_list = []
            requires_password = False

            if file_ext == 'zip':
                try:
                    with zipfile.ZipFile(path, 'r') as probe_archive:
                        # ZIP 可直接从标志位判断文件是否加密
                        requires_password = any(
                            (info.flag_bits & 0x1) and not info.is_dir()
                            for info in probe_archive.infolist()
                        )
                except Exception as e:
                    return False, f'无法读取 ZIP 文件：{e}'

            elif file_ext == '7z':
                if py7zr is None:
                    return False, '不支持 7z 格式，请安装 py7zr 库'
                try:
                    with py7zr.SevenZipFile(path, 'r') as probe_archive:
                        # 新版本 py7zr 提供 needs_password()，旧版本退化为探测读取目录
                        if hasattr(probe_archive, 'needs_password'):
                            requires_password = bool(probe_archive.needs_password())
                        else:
                            try:
                                probe_archive.getnames()
                                requires_password = False
                            except Exception:
                                requires_password = True
                except Exception as e:
                    return False, f'无法读取 7z 文件：{e}'

            elif file_ext == 'rar':
                if rarfile is None:
                    return False, '不支持 RAR 格式，请安装 rarfile 库'
                try:
                    with rarfile.RarFile(path, 'r') as probe_archive:
                        file_infos = [info for info in probe_archive.infolist() if not info.isdir()]
                        requires_password = any(info.needs_password() for info in file_infos)
                except Exception as e:
                    return False, f'无法读取 RAR 文件：{e}'
            else:
                return False, f'不支持的压缩格式：{file_ext}'

            # 先处理密码输入
            password = None
            if requires_password:
                password_dialog = PasswordInputDialog(self, basename(path))
                if password_dialog.exec() != QDialog.DialogCode.Accepted:
                    return False, '用户取消了操作'
                password = password_dialog.password

            # 先验证密码可用，再获取文件列表
            try:
                self.switch_button_states(False)
                if file_ext == 'zip':
                    archive = zipfile.ZipFile(path, 'r')
                    file_list = archive.namelist()

                    if requires_password:
                        encrypted_files = [
                            info.filename
                            for info in archive.infolist()
                            if (info.flag_bits & 0x1) and not info.is_dir()
                        ]
                        if encrypted_files:
                            if not password:
                                return False, '压缩包需要密码'
                            try:
                                with archive.open(encrypted_files[0], pwd=password.encode('utf-8')) as f:
                                    f.read(1)
                            except RuntimeError as e:
                                return False, f'密码错误或 ZIP 文件损坏：{e}'

                elif file_ext == '7z':
                    archive = py7zr.SevenZipFile(path, 'r', password=password or None)
                    file_list = archive.getnames()

                    if requires_password and not password:
                        return False, '压缩包需要密码'

                    first_file = next((f for f in file_list if not f.endswith('/')), None)
                    if first_file:
                        with tempfile.TemporaryDirectory(prefix='ci-7z-check-') as check_dir:
                            archive.reset()
                            archive.extract(path=check_dir, targets=[first_file])

                elif file_ext == 'rar':
                    archive = rarfile.RarFile(path, 'r')
                    file_list = archive.namelist()

                    if requires_password and not password:
                        return False, '压缩包需要密码'

                    first_file = next((f for f in file_list if not f.endswith('/')), None)
                    if first_file:
                        try:
                            with archive.open(first_file, 'r', pwd=password):
                                pass
                        except Exception as e:
                            return False, f'密码错误或 RAR 文件损坏：{e}'

            except Exception as e:
                return False, f'无法验证压缩包密码或读取文件列表：{e}'

            finally:
                self.switch_button_states(True)
                if archive:
                    archive.close()

            # 在密码验证通过后，再检查压缩包内是否存在符合格式的文件
            valid_files = [f for f in file_list if f.lower().endswith(('.cia', '.3ds', '.cci'))]
            if not valid_files:
                return False, '压缩包内没有找到 .cia/.3ds/.cci 文件'

            self.log(f'找到 {len(valid_files)} 个符合格式的文件')

            # 创建临时文件夹
            timestamp = str(int(time() * 1000))
            temp_dir = join(dirname(abspath(__file__)), f'ci-install-temp-{timestamp}')

            previous_add_cia_state = self.add_cia_button.isEnabled()

            def update_extract_progress(current: int, total: int, text: str = '解压中'):
                total = max(total, 1)
                percent = int(current / total * 100)
                self.progress_bar.setValue(percent)
                self.progress_bar_text.setText(f'{text}: {current}/{total} ({percent}%)')
                QApplication.processEvents()

            try:
                os.makedirs(temp_dir, exist_ok=True)
                self.log(f'创建临时目录：{temp_dir}')
            except Exception as e:
                return False, f'无法创建临时目录：{e}'

            # 解压文件
            try:
                self.switch_button_states(False)
                QApplication.processEvents()

                if file_ext == 'zip':
                    with zipfile.AESZipFile(path, 'r') as archive:
                        members = [info for info in archive.infolist() if not info.is_dir()]
                        total_members = len(members)
                        for index, info in enumerate(members, 1):
                            if password:
                                archive.extract(info, path=temp_dir, pwd=password.encode('utf-8'))
                            else:
                                archive.extract(info, path=temp_dir)
                            update_extract_progress(index, total_members)

                elif file_ext == '7z':
                    with py7zr.SevenZipFile(path, 'r', password=password or None) as archive:
                        members = [name for name in archive.getnames() if not name.endswith('/')]
                        total_members = len(members)
                        for index, member in enumerate(members, 1):
                            archive.extract(path=temp_dir, targets=[member])
                            update_extract_progress(index, total_members)

                elif file_ext == 'rar':
                    with rarfile.RarFile(path, 'r') as archive:
                        members = [info for info in archive.infolist() if not info.isdir()]
                        total_members = len(members)
                        for index, info in enumerate(members, 1):
                            archive.extract(info, path=temp_dir, pwd=password if password else None)
                            update_extract_progress(index, total_members)

                self.log(f'已解压到临时目录：{temp_dir}')

            except (RuntimeError, EOFError) as e:
                # 清理临时目录
                try:
                    shutil.rmtree(temp_dir)
                except:
                    pass
                return False, f'解压失败，可能密码错误或文件损坏：{e}'

            except Exception as e:
                # 清理临时目录
                try:
                    shutil.rmtree(temp_dir)
                except:
                    pass
                return False, f'解压失败：{e}'

            finally:
                self.progress_bar.setValue(0)
                self.progress_bar_text.setText('')
                QApplication.processEvents()
                self.switch_button_states(True)

            # 将解压后的文件夹传递给_add_folder，并设置delete=True以便处理完后删除
            self.log('将临时目录中的文件添加到列表...')
            self._add_folder(temp_dir, delete=True)

            return True, ''

        except Exception as e:
            self.log(f'处理压缩文件时出错：{e}')
            traceback.print_exc()
            return False, f'处理压缩文件时出错：{e}'

    def add_cia(self, path: str) -> Tuple[bool, str]:
        try:
            path = path.replace('\\', '/')
            
            # 检查是否是压缩文件
            compressed_extensions = ('.zip', '.7z', '.rar')
            if path.lower().endswith(compressed_extensions):
                return self.add_compressed_file(path)
            
            if path.lower().endswith('.3ds') or path.lower().endswith('.cci'):
                return self.add_game_card_image(path)
            with self.lock:
                if path in self.readers:
                    return False, '应用已添加在列表中'

                if path.lower().endswith('.cia'):
                    reader = CIAReader(path)
                else:
                    reader = CDNReader(path)


                if reader.tmd.title_id.startswith('00048'):
                    return False, '不支持 DSiWare 应用'

                if self.title_list.findItems(reader.tmd.title_id, Qt.MatchFlag.MatchExactly, column=2):
                    return False, '应用已添加在列表中'

                self.readers[path] = reader

                # Get title name
                try:
                    title_name = reader.contents[0].exefs.icon.get_app_title().short_desc
                except:
                    title_name = '（没有软件名）'

                # Get icon
                try:
                    icon = reader.contents[0].exefs.icon.icon_large
                    icon_data = BytesIO()
                    icon.save(icon_data, format='PNG')
                    pixmap = QPixmap()
                    pixmap.loadFromData(icon_data.getvalue())
                except:
                    traceback.print_exc()
                    pixmap = QPixmap()  # Empty pixmap if no icon

                # Get cover art
                try:
                    cover_art = reader.contents[0].exefs.icon.icon_large
                    cover_art_data = BytesIO()
                    cover_art.save(cover_art_data, format='PNG')
                    cover_art_pixmap = QPixmap()
                    cover_art_pixmap.loadFromData(cover_art_data.getvalue())
                except:
                    self.log("无法加载" + title_name + "的缩略图，跳过加载。" )
                    traceback.print_exc()
                    cover_art_pixmap = QPixmap()  # Empty pixmap if no cover art

                title_size = get_install_size(reader)

                # Check if adding this CIA would exceed available SD card capacity
                sd_path = self.sd_path.text()
                if sd_path and isdir(sd_path):
                    try:
                        usage = shutil.disk_usage(sd_path)
                        free_space = usage.free
                        total_app_size = self.get_total_app_size()
                        new_total_size = total_app_size + title_size

                        if new_total_size > free_space:
                            # Remove reader from the dictionary before rejecting
                            del self.readers[path]
                            error_msg = (f'SD 卡可用容量不足！\n\n'
                                       f'待添加应用大小: {format_file_size(title_size)}\n'
                                       f'SD 卡可用容量: {format_file_size(free_space)}\n\n'
                                       f'缺少空间: {format_file_size(new_total_size - free_space)}')
                            return False, error_msg
                    except Exception as e:
                        self.log(f'检查 SD 卡容量失败: {e}')

                item = QTreeWidgetItem([
                    '',
                    path,
                    str(reader.tmd.title_id).upper(),
                    title_name,
                    format_file_size(title_size),
                    statuses.get(InstallStatus.Waiting)
                ])

                self.title_list.addTopLevelItem(item)

                display_pixmap = cover_art_pixmap if not cover_art_pixmap.isNull() else pixmap
                if not display_pixmap.isNull():
                    scaled_pixmap = display_pixmap.scaled(
                        self.title_icon_size,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                    icon_label = QLabel()
                    icon_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
                    icon_label.setPixmap(scaled_pixmap)
                    icon_label.setStyleSheet('background: transparent; border: none;')
                    icon_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
                    self.title_list.setItemWidget(item, 0, icon_label)
                self.update_info_label()
                return True, ''

        except CIAError as e:
            return False, f'无法读取 CIA：{e}'
        except CDNError as e:
            return False, f'无法读取 CDN：{e}'
        except TitleMetadataError as e:
            return False, f'无法读取 TMD：{e}'
        except Exception as e:
            return False, str(e)

    def add_game_card_image(self, path: str):
        changed_button = False
        try:
            with self.lock:
                path = path.replace('\\', '/')
                if not self.skip_game_card_confirm:
                    info = QMessageBox(self)
                    info.setWindowTitle("添加游戏卡镜像")
                    info.setText(f"{path} 是一个游戏卡镜像文件。\n"
                                 f"本工具可以帮你预先转换好文件为 CIA 格式，但这需要一点时间转换。\n"
                                 f"是否继续？")
                    yes_button = info.addButton("是", QMessageBox.ButtonRole.YesRole)
                    all_yes_button = info.addButton("全是（本次安装）", QMessageBox.ButtonRole.YesRole)
                    no_button = info.addButton("否", QMessageBox.ButtonRole.NoRole)
                    info.setDefaultButton(yes_button)
                    info.exec()

                    clicked_button = info.clickedButton()
                    if clicked_button == no_button:
                        self.log('取消添加游戏卡镜像：' + path)
                        return
                    if clicked_button == all_yes_button:
                        self.skip_game_card_confirm = True
                self.switch_button_states(False)
                changed_button = True
                timestamp = str(int(time() * 1000))
                tmp_dir = str(Path(file_parent) / f'ci-install-temp-{timestamp}').replace('\\', '/')
                # Check free space on the drive where the temp folder will be created.
                try:
                    # Determine the drive/root for the tmp_dir (works on Windows and POSIX)
                    drive_root = Path(tmp_dir).anchor or Path(tmp_dir).drive or tmp_dir
                    # Estimate required space as the size of the source game card image
                    required_size = os.path.getsize(path) if isfile(path) else 0
                    usage = shutil.disk_usage(drive_root)
                    free_space = usage.free
                    if required_size > free_space:
                        # Alert the user and abort adding this game card image
                        msg = (f'临时目录所在磁盘可用容量不足，无法在此处转换文件。\n\n'
                               f'源文件大小: {format_file_size(required_size)}\n'
                               f'可用空间: {format_file_size(free_space)}\n\n'
                               f'请清理磁盘或选择其他位置启动程序后重试。')
                        self.log('取消转换游戏卡镜像，磁盘空间不足：' + path)
                        return False, msg
                except Exception as e:
                    # If we cannot determine disk usage for any reason, log and continue.
                    self.log(f'检查临时目录磁盘容量失败: {e}')
                os.makedirs(tmp_dir, exist_ok=True)
                self.log(f'正在转换游戏卡镜像 {path} 为 CIA 格式...')
                conventer(log=self.log,
                          verbose=True,
                          game=[path],
                          output=tmp_dir,
                          boot9=self.boot9_path.text(),
                          ignore_bad_hashes=self.force_install,
                          on_progress=lambda percent, read, size: self.signals.convert_progress_signal.emit(percent, read, size))
            self.log(f'转换完成，已缓存到 {tmp_dir}。正在添加到列表中...')

            self._add_folder(tmp_dir, delete=True)
            self.log(f'将缓存文件添加到待删除列表。')
        except Exception as e:
            self.log(f'无法添加游戏卡镜像：{e}')
            self.log(traceback.format_exc())
            return False, str(e)
        finally:
            self.progress_bar.reset()
            self.progress_bar_text.setText("")
            if changed_button:
                self.switch_button_states(True)
        return True, ''

    # Drag and drop support
    def dragEnterEvent(self, e: QDragEnterEvent):
        e.accept()

    def dropEvent(self, e):
        if not (e.mimeData().hasText() and self.add_cia_button.isEnabled()):
            return QMessageBox.warning(self, "错误", "请先选择 SD 卡根目录及 movable.sed。")
        filePathList = e.mimeData().text()
        filePath = filePathList.split('\n')
        cias = []
        dirs = []
        cards = []
        for p in filePath:
            p = p.replace('file:///', '', 1).strip()
            if p and isfile(p):
                if p.lower().endswith('.cia'):
                    cias.append(p)
                if p.lower().endswith('.3ds') or p.lower().endswith('.cci'):
                    cards.append(p)
                if p.lower().endswith(".zip") or p.lower().endswith(".7z") or p.lower().endswith(".rar"):
                    cias.append(p)
            elif p and isdir(p):
                dirs.append(p)
        if cias:
            self._add_cias(cias)
        if dirs:
            for d in dirs:
                self._add_folder(d)
        if cards:
            for card in cards:
                self.add_game_card_image(card)

    def check_b9_loaded(self):
        self.b9_loaded = False
        try:
            crypto = CryptoEngine(boot9=self.boot9_path.text() if self.boot9_path.text() else None)
            self.b9_loaded = crypto.b9_keys_set
        except MissingSeedError:
            pass
        except BootromNotFoundError:
            self.log('未找到 boot9.bin 文件，请指定一个文件。')
        except Exception as e:
            self.log(f'无法加载 boot9 文件：{e}')
        return self.b9_loaded


    def switch_button_states(self, enabled: bool):
        self.add_cia_button.setEnabled(enabled)
        self.add_cdn_button.setEnabled(enabled)
        self.add_folder_button.setEnabled(enabled)
        self.remove_button.setEnabled(enabled)
        self.start_button.setEnabled(enabled)

    def get_total_app_size(self) -> int:
        """Calculate total size of all applications in the list."""
        total_size = 0
        for i in range(self.title_list.topLevelItemCount()):
            item = self.title_list.topLevelItem(i)
            path = item.text(1)
            if path in self.readers:
                try:
                    total_size += get_install_size(self.readers[path])
                except:
                    pass
        return total_size

    def update_info_label(self):
        """Update the info label with SD path info and total application size."""
        sd_path = self.sd_path.text()
        total_str, free_str = get_disk_info(sd_path, self.log)

        total_app_size = self.get_total_app_size()
        app_size_str = format_file_size(total_app_size)

        if total_str and free_str:
            info_text = f'SD 卡总大小: {total_str} | 可用容量: {free_str} | 列表应用总大小: {app_size_str}'
        else:
            info_text = f'列表应用总大小: {app_size_str}'

        self.info_label.setText(info_text)

    def _update_button_states(self):
        self.enabled_button = all([self.check_b9_loaded(),
                       self.sd_path.text(),
                       self.movable_path.text(),
                       self.seeddb_path.text()])

        self.switch_button_states(self.enabled_button)
        self.update_info_label()

        if self.enabled_button:
            self.status_label.setText('就绪，可拖拽文件或文件夹至窗口添加应用（*.cia / *.3ds / *.cci）')
        else:
            self.status_label.setText('请选择 SD 卡根目录及 movable.sed。')
        return self.enabled_button


    def update_button_states(self):
        # anti debounce
        d = debounce(self._update_button_states, 0.5)
        d()
        self.repaint()


    def log(self, *msg, end='\n'):
        timestamp = datetime.now().strftime('%H:%M:%S')
        log_msg = f"{timestamp} - {end.join(msg)}"
        self.signals.log_signal.emit(log_msg)

    def on_log(self, message: str):
        self.log_window.append(message)
        # Make sure the latest log is visible
        self.log_window.verticalScrollBar().setValue(
            self.log_window.verticalScrollBar().maximum()
        )
        self.repaint()

    def save_log(self):
        confirm = QMessageBox.question(
            self, "导出日志", "你确定要导出日志吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if confirm == QMessageBox.StandardButton.Yes:
            timestamp = datetime.now().strftime('%H-%M-%S')
            logs_path = Path(os.path.abspath('.')) / 'logs'
            logs_path.mkdir(exist_ok=True)
            save_path = logs_path / f'custom-install-{timestamp}.log'
            with open(save_path, 'w', encoding='utf-8') as f:
                f.write(self.log_window.toPlainText())

            QMessageBox.information(self, '成功', f'日志已保存到 {save_path}！')
            os.startfile(str(save_path.parent))


    def on_progress(self, total_percent: float, total_read: int, size: int):
        self.progress_bar.setValue(int(total_percent))
        if taskbar:
            max_percentage = 100 * self.total_items
            taskbar.SetProgressValue(int(self.winId()), int(total_percent + self.finished_percent), max_percentage)

    def on_convert_progress(self, percent: float, read: int, size: int):
        """处理转换进度"""
        self.progress_bar.setValue(int(percent))
        self.progress_bar_text.setText(f"转换进度: {percent:.1f}% ({read} / {size})")
        if taskbar:
            taskbar.SetProgressValue(int(self.winId()), int(percent), 100)

    def on_error(self, exc: Exception):
        self.log(f'错误：{exc}')
        self.signals.log_signal.emit(f"错误：{str(exc)}")

    def on_cia_start(self, idx: int):
        self.log(f'开始安装第 {idx + 1} 个 CIA...')
        self.progress_bar_text.setText(f"正在安装第 {idx + 1} 个应用...")
        find_item = self.title_list.topLevelItem(idx)
        if find_item:
            find_item.setText(4, statuses.get(InstallStatus.Starting))
        if taskbar:
            self.finished_percent = idx * 100
            max_percentage = 100 * self.total_items
            taskbar.SetProgressValue(int(self.winId()), self.finished_percent, max_percentage)

    def on_status_update(self, path: str, status: InstallStatus):
        status_text = status.name if isinstance(status, InstallStatus) else str(status)
        cn_text = {
            'Waiting': '等待中',
            'Starting': '安装中',
            'Writing': '写入中',
            'Finishing': '完成中',
            'Done': '完成',
            'Failed': '失败',
            'Warning': '警告',
        }
        self.log(f'状态更新 {path}：{cn_text[status_text]}')
        # Find and update the item in the tree widget
        items = self.title_list.findItems(path, Qt.MatchFlag.MatchExactly, 1)
        if items:
            items[0].setText(4, cn_text[status_text])

    def on_installed_signal(self, lst: List[str], copied: bool, application_count: int):
        tex = '已完成安装。\n'
        if copied:
            tex += "custom-install-finalize 已被复制到 SD 卡。\n"
        root_ = self.sd_path.text()
        lst_dir = os.listdir(root_)
        if 'boot.firm' not in lst_dir or 'boot.3dsx' not in lst_dir:
            tex += ("重要警告：SD 卡根目录中未找到 boot.firm 或 boot.3dsx 文件。\n"
                    "请确保你已将 boot.firm 或 boot.3dsx 文件放在 SD 卡根目录中，以便能够正常启动完成安装程序。（custom-install-finalize）\n")
        if application_count > 300:
            tex += "注意：安装的应用数量超过 300 个，主机可能会无法正常显示所有应用。\n\n"
        tex += '成功安装了下列应用：\n'
        dial = ListBoxDialog(self, "以下应用已成功安装", tex, lst)
        dial.show()


    def on_failed_signal(self, lst: List[str]):
        if not lst:
            return
        tex = '以下应用安装失败，请检查输出查看问题：\n'
        dial = ListBoxDialog(self, "以下应用安装失败", tex, lst)
        dial.show()

    pending_remove = []

    def on_remove_signal(self):
        if self.pending_remove:
            for path in self.pending_remove:
                if os.path.exists(path):
                    try:
                        os.remove(path)
                        self.log(f"已删除 {path}")
                        _p = Path(path)
                        if _p.parent.exists() and _p.parent.name.startswith('ci-install-temp'):
                            if not any(_p.parent.iterdir()):
                                _pp = str(_p.parent).replace("\\", "/")
                                self.log(f'目录 {_pp} 为空，尝试删除...')
                                _p.parent.rmdir()
                                self.log(f'已删除空目录：{_pp}')
                    except Exception as e:
                        self.log(f"无法删除 {path}：{str(e)}")
            self.pending_remove.clear()

    def closeEvent(self, event):
        self.title_list.clear()
        self.readers.clear()
        if self.pending_remove:
            self.signals.remove_signal.emit()
            sleep(len(self.pending_remove) * 0.1)
        event.accept()

    def on_force_install_signal(self, force: bool):
        self.force_install = force
        if force:
            self.log("已启用强制安装模式。请注意，这很可能会导致安装的应用无法正常工作。")
        else:
            self.log("已禁用强制安装模式。")

    def on_export_finalize_signal(self):
        if not self.sd_path.text():
            QMessageBox.warning(self, "错误", "请先选择 SD 卡根目录。")
            return
        src = Path(file_parent) / 'custom-install-finalize.3dsx'
        dst = Path(self.sd_path.text()) / '3ds' / 'custom-install-finalize.3dsx'
        try:
            shutil.copy(src, dst)
            QMessageBox.information(self, "成功", f"custom-install-finalize 已导出到 {dst}。")
        except Exception as e:
            QMessageBox.critical(self, "错误", f"导出 custom-install-finalize 失败：{str(e)}")
            self.log(f"导出 custom-install-finalize 失败：{str(e)}")

    def on_recover_pending_install_signal(self):
        if not self.sd_path.text():
            QMessageBox.warning(self, "错误", "请先选择 SD 卡根目录。")
            return
        pending_path = Path(self.sd_path.text()) / 'ci-pending'
        if not pending_path.exists() or not pending_path.is_dir():
            QMessageBox.warning(self, "错误", f"未找到 ci-pending 文件夹：{pending_path}")
            return
        try:
            for p in pending_path.iterdir():
                self.log('正在恢复未完成的安装：' + str(p))
                shutil.move(str(p), str(pending_path / '..'))
        except Exception:
            self.log(f"恢复未完成的安装失败：")
            self.log(traceback.format_exc())
        QMessageBox.information(self, "信息", "已尝试恢复未完成的安装。请检查控制台输出。")
        shutil.rmtree(pending_path)


    def on_delete_corrupted_files_signal(self):
        if not self.sd_path.text():
            QMessageBox.warning(self, "错误", "请先选择 SD 卡根目录。")
            return
        deleted = False
        for p in Path(self.sd_path.text()).glob('ci-install-temp*'):
            deleted = True
            if p.is_dir():
                try:
                    shutil.rmtree(p)
                    self.log(f"已删除损坏的文件夹：{p}")
                except Exception as e:
                    self.log(f"无法删除 {p}：{str(e)}")
            elif p.is_file():
                try:
                    p.unlink()
                    self.log(f"已删除损坏的文件：{p}")
                except Exception as e:
                    self.log(f"无法删除 {p}：{str(e)}")
        if deleted:
            QMessageBox.information(self, "成功", "已删除所有损坏的文件。")
        else:
            QMessageBox.information(self, "失败", "未找到任何损坏的文件。")

    def on_finished_signal(self):
        try:
            self.title_list.clear()
            self.readers.clear()
            # Re-enable install button
            self.start_button.setEnabled(True)
            self.switch_button_states(True)
            self.progress_bar_text.setText('')
            self.progress_bar.reset()
            self.skip_game_card_confirm = False

            if taskbar:
                taskbar.SetProgressState(int(self.winId()), tbl.TBPF_NOPROGRESS)

            self.signals.remove_signal.emit()

            # Show notification
            QApplication.alert(self, 60000)
            self.tray_icon.showMessage(
                "custom install安装完成",
                "请检查窗口以获取安装结果。",
                QSystemTrayIcon.MessageIcon.Information,
                2000  # Duration in milliseconds
            )
        except Exception as e:
            self.log(f"清理安装状态时发生错误：{str(e)}")

    def start_install(self):
        if not self.readers:
            self.signals.log_signal.emit("你还没有添加任何应用，请先添加一个再进行安装。")
            return

        # Disable install button
        self.start_button.setEnabled(False)
        self.status_label.setText(statuses.get(InstallStatus.Starting))
        self.log("开始安装...")
        self.install_thread = Thread(target=self.install)
        self.install_thread.start()


    def install(self):
        try:
            self.switch_button_states(False)
            self.log("准备安装中...")
            # Get the SD root path
            sd_path = self.sd_path.text()
            if not sd_path:
                raise Exception("未指定 SD 卡根目录")

            # Get the movable.sed path
            movable_path = self.movable_path.text()
            if not movable_path:
                raise Exception("未指定 movable.sed")

            if taskbar:
                taskbar.SetProgressState(int(self.winId()), tbl.TBPF_NORMAL)

            # Create CustomInstall instance
            custom_install = CustomInstall(
                boot9=self.boot9_path.text() if self.boot9_path.text() else None,
                seeddb=self.seeddb_path.text() if self.seeddb_path.text() else None,
                movable=movable_path,
                sd=sd_path,
                skip_contents=self.skip_contents.isChecked(),
                overwrite_saves=self.overwrite_saves.isChecked(),
                force_install=self.force_install
            )

            # Set up event handlers
            custom_install.event.on_log_msg += lambda msg, **kwargs: self.signals.log_signal.emit(str(msg))
            custom_install.event.update_percentage += lambda total_percent, total_read, size: self.signals.progress_signal.emit(total_percent, int(total_read), int(size))
            custom_install.event.on_error += lambda exc: self.signals.error_signal.emit(exc)
            custom_install.event.on_cia_start += lambda idx: self.signals.cia_start_signal.emit(idx)
            custom_install.event.update_status += lambda path, status: self.signals.status_signal.emit(path, status)

            # Prepare readers in the order they appear in the tree
            root = self.title_list.invisibleRootItem()
            self.total_items = root.childCount()

            self.log(f"找到了 {self.total_items} 个应用")

            # Create list of readers in the order they appear in the tree
            for i in range(self.total_items):
                item = root.child(i)
                path = item.text(1)
                if self.install_and_delete.isChecked():
                    self.pending_remove.append(path)
                if path in self.readers:
                    custom_install.readers.append((self.readers[path], path))
                    self.log(f"为安装准备 {path} 中...")

            # Check for id0
            if not custom_install.check_for_id0():
                raise Exception(f'SD 卡的 “Nintendo 3DS” 文件夹中找不到 id0 {custom_install.crypto.id0.hex()} 文件夹。\n'
                            f'\n'
                            f'在使用 custom-install 前，你应先确保这张 SD 卡的格式为 FAT32，且插入主机开机过一次。\n'
                            f'\n'
                            f'或者，请确保你使用了正确的 movable.sed 文件。')

            # Start the installation
            self.log("开始安装应用...")
            install_state, copied, application_count = custom_install.start()
            if iend := install_state.get('installed'):
                self.signals.installed_signal.emit(iend, copied, application_count)
                self.log(f"成功安装了 {len(iend)} 个应用。")

            if failed := install_state.get('failed'):
                self.signals.failed_signal.emit(failed)
                self.log(f"{len(failed)} 个应用安装失败。请查看日志以获取更多信息。")


            self.signals.log_signal.emit("安装完成。")
            self.status_label.setText('安装完成。')

        except Exception as e:
            self.signals.log_signal.emit(f"安装失败：{str(e)}")
            self.signals.error_signal.emit(e)
            self.status_label.setText('安装失败。')
        finally:
            self.signals.finished_signal.emit()


def main():
    app = QApplication(sys.argv)
    icon = QIcon(file_parent + '/logo.ico')
    app.setWindowIcon(icon)
    window = CustomInstallGUI()
    window.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()