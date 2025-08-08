#!/usr/bin/env python3
import os
import shutil
import sys
import traceback
from datetime import datetime
from io import BytesIO
from os import environ
from os.path import abspath, basename, dirname, join, isfile, isdir
from pathlib import Path
from threading import Thread, Lock
from threading import Timer
from typing import Tuple, List, Dict

from PyQt6.QtCore import Qt, pyqtSignal, QObject, QSize, QUrl
from PyQt6.QtGui import QPixmap, QIcon
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
                             QLabel, QLineEdit, QPushButton, QFileDialog, QTreeWidget,
                             QTreeWidgetItem, QProgressBar, QCheckBox, QMessageBox,
                             QTextEdit, QSplitter, QDialog, QAbstractItemView, QSystemTrayIcon)
from pyctr.crypto import MissingSeedError, CryptoEngine, load_seeddb
from pyctr.crypto.engine import b9_paths, BootromNotFoundError
from pyctr.type.cdn import CDNError, CDNReader
from pyctr.type.cia import CIAError, CIAReader
from pyctr.type.tmd import TitleMetadataError
from pyctr.util import config_dirs

from custominstall import CustomInstall, load_cifinish, InvalidCIFinishError, InstallStatus, CI_VERSION, is_windows

# This file is a part of custom-install.py.
#
# custom-install is copyright (c) 2019-2020 Ian Burgwin
# This file is licensed under The MIT License (MIT).
# You can find the full license text in LICENSE.md in the root of this project.

file_parent = dirname(abspath(__file__))

CI_VERSION = 'OasisAkari Modded 1.2'


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
        pass

def find_first_file(paths):
    for p in paths:
        if isfile(p):
            return p


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


class InstallSignals(QObject):
    log_signal = pyqtSignal(str)
    progress_signal = pyqtSignal(float, int, int)
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


        # Add close button
        close_button = QPushButton("关闭")
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
        self.setup_file_pickers()
        self.setup_title_buttons()
        self.setup_title_list()
        self.setup_progress()
        self.setup_controls()

        # textChanged signals
        self.sd_path.textChanged.connect(self.update_button_states)
        self.movable_path.textChanged.connect(self.update_button_states)
        self.seeddb_path.textChanged.connect(self.update_button_states)
        self.boot9_path.textChanged.connect(self.update_button_states)

        # Setup signals
        self.signals.log_signal.connect(self.on_log)
        self.signals.progress_signal.connect(self.on_progress)
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

        # Initial state
        self.log(f'custom-install {CI_VERSION} - https://github.com/ihaveamac/custom-install')
        self.log(f'汉化 & 修改 By OasisAkari （一只火狐） - https://stray-soul.com/，请勿二次出售（如闲鱼等平台）与商用。')
        self.log("就绪。")

        if taskbar:
            # Set up taskbar button
            taskbar.ActivateTab(int(self.winId()))

        # if is_mica_supported():
        #     self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        #     hwnd = int(self.winId())
        #     ApplyMica(hwnd, MicaType.MICA)

        self.force_install = False
        self.total_items = 0
        self.finished_percent = 0
        self.tray_icon = QSystemTrayIcon(self.windowIcon(), self)
        self.tray_icon.show()


    def setup_file_pickers(self):
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


    def setup_title_buttons(self):
        button_layout = QHBoxLayout()

        self.add_cia_button = QPushButton('添加 CIA')
        self.add_cia_button.setEnabled(False)
        self.add_cia_button.clicked.connect(self.add_cias)
        button_layout.addWidget(self.add_cia_button)

        self.add_cdn_button = QPushButton('添加 CDN 应用目录')
        self.add_cdn_button.setEnabled(False)
        self.add_cdn_button.clicked.connect(self.add_cdn)
        button_layout.addWidget(self.add_cdn_button)

        self.add_folder_button = QPushButton('添加 CIA 文件夹')
        self.add_folder_button.setEnabled(False)
        self.add_folder_button.clicked.connect(self.add_folder)
        button_layout.addWidget(self.add_folder_button)

        self.remove_button = QPushButton('移除选择的内容（按住CTRL键多选）')
        self.remove_button.setEnabled(False)
        self.remove_button.clicked.connect(self.remove_selected)
        button_layout.addWidget(self.remove_button)

        self.layout.addLayout(button_layout)


    def setup_title_list(self):
        # Create a splitter for the tree view and log window
        self.splitter = QSplitter()
        self.splitter.setOrientation(Qt.Orientation.Vertical)
        self.layout.addWidget(self.splitter)

        # Title list
        self.title_list = QTreeWidget()
        self.title_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.title_list.setHeaderLabels(['图标', '文件路径', '应用 ID', '应用名', '安装状态'])
        self.title_list.setColumnWidth(0, 50)
        self.title_list.setColumnWidth(1, 200)
        self.title_list.setColumnWidth(2, 150)
        self.title_list.setColumnWidth(3, 300)
        self.title_list.setColumnWidth(4, 10)
        self.splitter.addWidget(self.title_list)

        # Log window
        self.log_window = QTextEdit()
        self.log_window.setReadOnly(True)
        self.log_window.setMinimumHeight(100)
        self.splitter.addWidget(self.log_window)

    def setup_progress(self):
        self.progress_bar_text = QLabel('')
        self.layout.addWidget(self.progress_bar_text)
        self.progress_bar = QProgressBar()
        self.progress_bar.setMaximum(100)
        self.layout.addWidget(self.progress_bar)

    def setup_controls(self):
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

        self.status_label = QLabel()
        self.layout.addWidget(self.status_label)

    def show_about(self):
        if not self.dialog:
            self.dialog = AboutDialog(self)
        self.dialog.show()
        self.dialog.setFixedSize(self.dialog.size())

    def select_sd_root(self):
        directory = QFileDialog.getExistingDirectoryUrl(self, "选择 SD 卡根目录", QUrl("clsid:0AC0837C-BBF8-452A-850D-79D08E667CA7"))
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


    def auto_detect_file(self, sd_root: str, filename: str) -> str:
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
                self.boot9_path.setText(file_name)
                self.check_b9_loaded()
            elif file_type == 'seeddb':
                self.seeddb_path.setText(file_name)
                load_seeddb(file_name)
            elif file_type == 'movable.sed':
                self.movable_path.setText(file_name)
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
            QMessageBox.warning(self, "无法添加应用", error_text)

    def add_cias(self):
        files, _ = QFileDialog.getOpenFileNames(self, "选择 CIA 文件", "", "CIA 文件 (*.cia)")
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

    def _add_folder(self, path):
        if not self.enabled_button:
            QMessageBox.warning(self, "错误", "请先选择 SD 卡根目录及 movable.sed。")
            return
        if path:
            failed = {}
            for root, dirs, files in os.walk(path):
                for file in files:
                    file_path = join(root, file)
                    if file_path.lower().endswith('.cia'):
                        success, reason = self.add_cia(str(file_path))
                        if not success:
                            failed[file_path] = reason
            if failed:
                error_text = "无法添加以下文件：\n\n"
                for path, reason in failed.items():
                    error_text += f"{basename(path)}: {reason}\n"
                QMessageBox.warning(self, "添加软件失败", error_text)

    def add_folder(self):
        directory = QFileDialog.getExistingDirectory(self, "选择包含了 CIA 文件的文件夹")
        self._add_folder(directory)

    def remove_selected(self):
        for item in self.title_list.selectedItems():
            self.title_list.takeTopLevelItem(self.title_list.indexOfTopLevelItem(item))
            path = item.text(1)
            if path in self.readers:
                del self.readers[path]

    def add_cia(self, path: str) -> Tuple[bool, str]:
        try:
            with self.lock:
                if path in self.readers:
                    return False, '应用已添加在列表中'

                if path.lower().endswith('.cia'):
                    reader = CIAReader(path)
                else:
                    reader = CDNReader(path)

                self.readers[path] = reader

                if reader.tmd.title_id.startswith('00048'):
                    return False, '不支持 DSiWare 应用'

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

                item = QTreeWidgetItem([
                    '',
                    path,
                    str(reader.tmd.title_id).upper(),
                    title_name,
                    statuses.get(InstallStatus.Waiting)
                ])

                # Set icon if available
                if not pixmap.isNull():
                    item.setIcon(0, QIcon(pixmap))

                # Set cover art if available
                if not cover_art_pixmap.isNull():
                    item.setIcon(0, QIcon(cover_art_pixmap))

                self.title_list.addTopLevelItem(item)
                return True, ''

        except CIAError as e:
            return False, f'无法读取 CIA：{e}'
        except CDNError as e:
            return False, f'无法读取 CDN：{e}'
        except TitleMetadataError as e:
            return False, f'无法读取 TMD：{e}'
        except Exception as e:
            return False, str(e)

    # Drag and drop support
    def dragEnterEvent(self, e):
        if e.mimeData().hasText() and self.add_cia_button.isEnabled():
            e.accept()
        else:
            e.ignore()

    def dropEvent(self, e):
        filePathList = e.mimeData().text()
        filePath = filePathList.split('\n')
        cias = []
        dirs = []
        for p in filePath:
            p = p.replace('file:///', '', 1).strip()
            if p and isfile(p):
                if p.lower().endswith('.cia'):
                    cias.append(p)
            elif p and isdir(p):
                dirs.append(p)
        if cias:
            self._add_cias(cias)
        if dirs:
            for d in dirs:
                self._add_folder(d)


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

    enabled_button = False

    def _update_button_states(self):
        self.enabled_button = all([self.check_b9_loaded(),
                       self.sd_path.text(),
                       self.movable_path.text(),
                       self.seeddb_path.text()])

        self.switch_button_states(self.enabled_button)

        if self.enabled_button:
            self.status_label.setText('就绪。（可拖拽文件或文件夹至窗口添加 CIA）')
        else:
            self.status_label.setText('请选择 SD 卡根目录及 movable.sed。')
        return self.enabled_button


    def update_button_states(self):
        # anti debounce
        d = debounce(self._update_button_states, 0.5)
        d()


    def log(self, message: str):
        timestamp = datetime.now().strftime('%H:%M:%S')
        log_msg = f"{timestamp} - {message}"
        self.signals.log_signal.emit(log_msg)

    def on_log(self, message: str):
        self.log_window.append(message)
        # Make sure the latest log is visible
        self.log_window.verticalScrollBar().setValue(
            self.log_window.verticalScrollBar().maximum()
        )

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
                try:
                    os.remove(path)
                    self.log(f"已删除 {path}")
                except Exception as e:
                    self.log(f"无法删除 {path}：{str(e)}")
            self.pending_remove.clear()

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
                overwrite_saves=self.overwrite_saves.isChecked()
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

            self.title_list.clear()
            self.readers.clear()
            # Re-enable install button
            self.start_button.setEnabled(True)
            self.switch_button_states(True)
            self.progress_bar_text.setText('')
            self.progress_bar.reset()

            if self.install_and_delete.isChecked():
                self.signals.remove_signal.emit()

            # Show notification
            QApplication.alert(self, 60000)
            self.tray_icon.showMessage(
                "custom install安装完成",
                "请检查窗口以获取安装结果。",
                QSystemTrayIcon.MessageIcon.Information,
                2000  # Duration in milliseconds
            )


def main():
    app = QApplication(sys.argv)
    icon = QIcon(file_parent + '/logo.ico')
    app.setWindowIcon(icon)
    window = CustomInstallGUI()
    window.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()