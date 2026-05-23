#!/usr/bin/env python3

import gi
gi.require_version('Gtk', '3.0')
gi.require_version('GtkSource', '4')
from gi.repository import Gtk, GLib, Gio, Gdk, Pango, GtkSource
import paramiko
import keyring
import json
import threading
import logging
from pathlib import Path
from typing import Dict, List, Optional
import asyncio
from dataclasses import dataclass
import subprocess
import socket
from rich.console import Console
from rich.text import Text
from io import StringIO
import os
import re

# Add at the top of the file, after imports
APP_NAME = "systemd Pilot"
APP_VERSION = "3.0.0"
APP_DESCRIPTION = """
A graphical tool for managing systemd services locally and remotely.

"""
APP_AUTHORS = ["mFat"]
APP_WEBSITE = "https://github.com/mfat/systemd-pilot"
APP_LICENSE = "GNU General Public License v3.0"
APP_ID = "io.github.mfat.systemdpilot"

@dataclass
class RemoteHost:
    name: str
    hostname: str
    username: str
    auth_type: str  # 'password' or 'key'
    key_path: Optional[str] = None

def get_system_theme_preference():
    """Detect system theme preference"""
    # Try gsettings first
    try:
        settings = Gio.Settings.new("org.gnome.desktop.interface")
        gtk_theme = settings.get_string("gtk-theme")
        return "dark" in gtk_theme.lower()
    except:
        pass
    
    # Fallback to environment variable
    gtk_theme = os.getenv("GTK_THEME", "")
    return "dark" in gtk_theme.lower()

class SystemdManagerWindow(Gtk.Window):
    def __init__(self):
        super().__init__(title=f"{APP_NAME} v{APP_VERSION}")
        self.set_default_size(1000, 600)
        
        # Initialize theme settings
        self.settings = Gtk.Settings.get_default()
        self.is_dark_mode = get_system_theme_preference()
        self.settings.set_property("gtk-application-prefer-dark-theme", self.is_dark_mode)
        
        # Initialize CSS provider
        self.css_provider = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(),
            self.css_provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        
        # Apply initial theme
        self.update_custom_theme()
        
        # Initialize logging with more detail
        logging.basicConfig(
            level=logging.DEBUG,
            format='%(asctime)s - %(levelname)s - %(message)s'
        )
        self.logger = logging.getLogger(__name__)
        self.logger.info("Starting Systemd Service Manager")
        
        # Store remote connections
        self.remote_hosts: Dict[str, RemoteHost] = {}
        self.active_connections: Dict[str, paramiko.SSHClient] = {}
        
        # Add near the start of __init__ with other initializations
        self.show_inactive = False  # Track whether to show inactive services
        self.show_user = False  # Track whether to show user services
        
        self.setup_ui()
        self.load_saved_hosts()

    def setup_ui(self):
        # Apply initial CSS theme
        self.update_custom_theme()
        
        # Main container
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.add(main_box)

        # Header bar
        header = Gtk.HeaderBar()
        header.set_show_close_button(True)
        
        # Set title on the far left
        header.set_title(APP_NAME)
        header.set_subtitle(None)  # Remove subtitle
        header.set_custom_title(None)  # Remove custom title
        
        # Mode switcher on the left
        mode_switch = Gtk.StackSwitcher()
        self.stack = Gtk.Stack()
        mode_switch.set_stack(self.stack)
        header.pack_start(mode_switch)  # Pack to the left
        
        # Add menu button on the right
        menu_button = Gtk.MenuButton()
        menu_button.set_image(Gtk.Image.new_from_icon_name("open-menu-symbolic", Gtk.IconSize.BUTTON))
        header.pack_end(menu_button)  # Pack to the right
        
        # Set window titlebar
        self.set_titlebar(header)

        # Create popover menu
        popover = Gtk.Popover()
        menu_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        menu_box.set_margin_start(12)
        menu_box.set_margin_end(12)
        menu_box.set_margin_top(6)
        menu_box.set_margin_bottom(6)
        
        # Create menu items
        create_service_btn = Gtk.Button(label="Create Service")
        reload_config_btn = Gtk.Button(label="Reload Configuration")
        show_inactive_btn = Gtk.Button(label="Show Inactive Services")  # New button
        show_user_btn = Gtk.Button(label="Show User Services")  # New button
        theme_btn = Gtk.Button(label="Toggle Dark Theme")
        about_btn = Gtk.Button(label="About")
        
        # Add icons to menu items
        create_service_btn.set_image(Gtk.Image.new_from_icon_name("document-new-symbolic", Gtk.IconSize.BUTTON))
        create_service_btn.set_always_show_image(True)
        reload_config_btn.set_image(Gtk.Image.new_from_icon_name("view-refresh-symbolic", Gtk.IconSize.BUTTON))
        reload_config_btn.set_always_show_image(True)
        show_inactive_btn.set_image(Gtk.Image.new_from_icon_name("view-list-symbolic", Gtk.IconSize.BUTTON))
        show_inactive_btn.set_always_show_image(True)
        show_user_btn.set_image(Gtk.Image.new_from_icon_name("view-list-symbolic", Gtk.IconSize.BUTTON))
        show_user_btn.set_always_show_image(True)
        theme_btn.set_image(Gtk.Image.new_from_icon_name("display-brightness-symbolic", Gtk.IconSize.BUTTON))
        theme_btn.set_always_show_image(True)
        about_btn.set_image(Gtk.Image.new_from_icon_name("help-about-symbolic", Gtk.IconSize.BUTTON))
        about_btn.set_always_show_image(True)
        
        # Connect signals
        create_service_btn.connect("clicked", self.show_create_service_dialog)
        reload_config_btn.connect("clicked", self.reload_systemd_config)
        show_inactive_btn.connect("clicked", self.toggle_show_inactive)
        show_user_btn.connect("clicked", self.toggle_show_user)
        theme_btn.connect("clicked", self.toggle_theme)
        about_btn.connect("clicked", self.show_about_dialog)
        
        # Add buttons to menu
        menu_box.pack_start(create_service_btn, False, False, 0)
        menu_box.pack_start(reload_config_btn, False, False, 0)
        menu_box.pack_start(show_inactive_btn, False, False, 0)
        menu_box.pack_start(show_user_btn, False, False, 0)
        menu_box.pack_start(theme_btn, False, False, 0)
        menu_box.pack_start(Gtk.Separator(), False, False, 3)
        menu_box.pack_start(about_btn, False, False, 0)
        
        # Show all widgets in the menu box
        menu_box.show_all()
        
        popover.add(menu_box)
        menu_button.set_popover(popover)

        # Local and Remote pages
        self.stack.add_titled(self.create_local_page(), "local", "Local")
        self.stack.add_titled(self.create_remote_page(), "remote", "Remote")
        
        main_box.pack_start(self.stack, True, True, 0)

    def create_remote_page(self):
        remote_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        
        # Left sidebar for host management
        sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        sidebar.set_size_request(250, -1)
        
        # Host management buttons with better spacing and icons
        host_buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        host_buttons.set_margin_start(6)
        host_buttons.set_margin_end(6)
        host_buttons.set_margin_top(6)
        host_buttons.set_margin_bottom(12)  # Add more bottom margin
        
        # Create buttons with icons
        add_host_btn = Gtk.Button(label="Add Host")
        add_host_btn.set_image(Gtk.Image.new_from_icon_name("list-add-symbolic", Gtk.IconSize.BUTTON))
        add_host_btn.set_always_show_image(True)
        
        edit_host_btn = Gtk.Button(label="Edit Host")
        edit_host_btn.set_image(Gtk.Image.new_from_icon_name("document-edit-symbolic", Gtk.IconSize.BUTTON))
        edit_host_btn.set_always_show_image(True)
        
        connect_btn = Gtk.Button(label="Connect")
        connect_btn.set_image(Gtk.Image.new_from_icon_name("network-transmit-symbolic", Gtk.IconSize.BUTTON))
        connect_btn.set_always_show_image(True)
        
        disconnect_btn = Gtk.Button(label="Disconnect")
        disconnect_btn.set_image(Gtk.Image.new_from_icon_name("network-offline-symbolic", Gtk.IconSize.BUTTON))
        disconnect_btn.set_always_show_image(True)
        
        # Connect signals
        add_host_btn.connect("clicked", self.show_add_host_dialog)
        edit_host_btn.connect("clicked", self.show_edit_host_dialog)
        connect_btn.connect("clicked", self.on_connect_clicked)
        disconnect_btn.connect("clicked", self.on_disconnect_clicked)
        
        # Add buttons to box with equal spacing
        host_buttons.pack_start(add_host_btn, True, True, 0)
        host_buttons.pack_start(edit_host_btn, True, True, 0)
        host_buttons.pack_start(connect_btn, True, True, 0)
        host_buttons.pack_start(disconnect_btn, True, True, 0)
        
        # Host list
        scrolled = Gtk.ScrolledWindow()
        self.hosts_list = Gtk.ListBox()
        self.hosts_list.connect("button-press-event", self.on_host_button_press)
        scrolled.add(self.hosts_list)
        
        sidebar.pack_start(scrolled, True, True, 0)
        sidebar.pack_start(host_buttons, False, False, 0)
        
        # Main content area
        content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        
        # Service list with descriptions
        self.remote_service_store = Gtk.ListStore(str, str, str, str)  # Name, Status, Host, Description
        self.remote_service_view = Gtk.TreeView(model=self.remote_service_store)
        
        # Create cell renderers with better formatting
        cell = Gtk.CellRendererText()
        cell.set_property("ypad", 6)
        cell.set_property("wrap-mode", Pango.WrapMode.WORD)
        cell.set_property("wrap-width", 300)
        
        # Create columns with better default widths and sorting
        name_col = Gtk.TreeViewColumn("Service")
        name_col.pack_start(cell, True)
        name_col.set_cell_data_func(cell, self.format_service_cell)
        name_col.set_resizable(True)
        name_col.set_expand(True)
        name_col.set_min_width(300)
        name_col.set_sort_column_id(0)  # Sort by service name
        
        status_col = Gtk.TreeViewColumn("Status", Gtk.CellRendererText(), text=1)
        status_col.set_resizable(True)
        status_col.set_fixed_width(120)
        status_col.set_sizing(Gtk.TreeViewColumnSizing.FIXED)
        status_col.set_sort_column_id(1)  # Sort by status
        
        host_col = Gtk.TreeViewColumn("Host", Gtk.CellRendererText(), text=2)
        host_col.set_resizable(True)
        host_col.set_fixed_width(150)
        host_col.set_sizing(Gtk.TreeViewColumnSizing.FIXED)
        host_col.set_sort_column_id(2)  # Sort by host
        
        self.remote_service_view.append_column(name_col)
        self.remote_service_view.append_column(status_col)
        self.remote_service_view.append_column(host_col)
        
        # Connect double-click and context menu
        self.remote_service_view.connect("row-activated", self.on_service_activated)
        self.remote_service_view.connect("button-press-event", self.on_service_button_press)
        
        service_scroll = Gtk.ScrolledWindow()
        service_scroll.add(self.remote_service_view)
        
        # Control buttons with styling
        control_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        control_box.set_margin_start(12)
        control_box.set_margin_end(12)
        control_box.set_margin_top(6)
        control_box.set_margin_bottom(6)
        
        # Add a subtle background
        control_box_style = control_box.get_style_context()
        control_box_style.add_class('action-bar')
        
        # Create styled buttons
        start_btn = self.create_action_button("Start", "media-playback-start-symbolic")
        stop_btn = self.create_action_button("Stop", "media-playback-stop-symbolic")
        restart_btn = self.create_action_button("Restart", "view-refresh-symbolic")
        enable_btn = self.create_action_button("Enable", "object-select-symbolic")
        disable_btn = self.create_action_button("Disable", "window-close-symbolic")
        logs_btn = self.create_action_button("View Logs", "text-x-generic-symbolic")
        
        # Connect signals
        start_btn.connect("clicked", self.on_start_service)
        stop_btn.connect("clicked", self.on_stop_service)
        restart_btn.connect("clicked", self.on_restart_service)
        enable_btn.connect("clicked", self.on_enable_service)
        disable_btn.connect("clicked", self.on_disable_service)
        logs_btn.connect("clicked", self.show_logs_dialog)
        
        # Add buttons with consistent spacing
        for btn in (start_btn, stop_btn, restart_btn, enable_btn, disable_btn, logs_btn):
            control_box.pack_start(btn, False, False, 0)
        
        content_box.pack_start(service_scroll, True, True, 0)
        content_box.pack_start(control_box, False, False, 0)
        
        remote_box.pack_start(sidebar, False, False, 0)
        remote_box.pack_start(content_box, True, True, 0)
        
        # Make the store sortable
        self.remote_service_store.set_sort_func(0, self.sort_by_name)
        self.remote_service_store.set_sort_func(1, self.sort_by_status)
        self.remote_service_store.set_sort_func(2, self.sort_by_host)
        
        return remote_box

    def create_local_page(self):
        local_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        
        # Create service list store and view with descriptions
        self.local_service_store = Gtk.ListStore(str, str, str)  # Name, Status, Description
        self.local_service_view = Gtk.TreeView(model=self.local_service_store)
        
        # Create cell renderers with better formatting
        cell = Gtk.CellRendererText()
        cell.set_property("ypad", 6)
        cell.set_property("wrap-mode", Pango.WrapMode.WORD)
        cell.set_property("wrap-width", 300)
        
        # Create columns with better default widths and sorting
        name_col = Gtk.TreeViewColumn("Service")
        name_col.pack_start(cell, True)
        name_col.set_cell_data_func(cell, self.format_local_service_cell)
        name_col.set_resizable(True)
        name_col.set_expand(True)
        name_col.set_min_width(300)
        name_col.set_sort_column_id(0)  # Sort by service name
        
        status_col = Gtk.TreeViewColumn("Status", Gtk.CellRendererText(), text=1)
        status_col.set_resizable(True)
        status_col.set_fixed_width(120)
        status_col.set_sizing(Gtk.TreeViewColumnSizing.FIXED)
        status_col.set_sort_column_id(1)  # Sort by status

        self.local_service_view.append_column(name_col)
        self.local_service_view.append_column(status_col)
        
        # Connect double-click and context menu
        self.local_service_view.connect("row-activated", self.on_local_service_activated)
        self.local_service_view.connect("button-press-event", self.on_local_service_button_press)
        
        # Add scrolled window
        scrolled = Gtk.ScrolledWindow()
        scrolled.add(self.local_service_view)
        
        # Control buttons with styling
        control_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        control_box.set_margin_start(12)
        control_box.set_margin_end(12)
        control_box.set_margin_top(6)
        control_box.set_margin_bottom(6)
        
        # Add a subtle background
        control_box_style = control_box.get_style_context()
        control_box_style.add_class('action-bar')
        
        # Create styled buttons
        start_btn = self.create_action_button("Start", "media-playback-start-symbolic")
        stop_btn = self.create_action_button("Stop", "media-playback-stop-symbolic")
        restart_btn = self.create_action_button("Restart", "view-refresh-symbolic")
        enable_btn = self.create_action_button("Enable", "object-select-symbolic")
        disable_btn = self.create_action_button("Disable", "window-close-symbolic")
        refresh_btn = self.create_action_button("Refresh", "view-refresh-symbolic")
        logs_btn = self.create_action_button("View Logs", "text-x-generic-symbolic")
        
        # Connect signals
        start_btn.connect("clicked", self.on_local_start_service)
        stop_btn.connect("clicked", self.on_local_stop_service)
        restart_btn.connect("clicked", self.on_local_restart_service)
        enable_btn.connect("clicked", self.on_local_enable_service)
        disable_btn.connect("clicked", self.on_local_disable_service)
        refresh_btn.connect("clicked", self.refresh_local_services)
        logs_btn.connect("clicked", self.show_local_logs_dialog)
        
        # Add buttons with consistent spacing
        for btn in (start_btn, stop_btn, restart_btn, enable_btn, disable_btn, refresh_btn, logs_btn):
            control_box.pack_start(btn, False, False, 0)
        
        local_box.pack_start(scrolled, True, True, 0)
        local_box.pack_start(control_box, False, False, 0)
        
        # Initial service load
        self.refresh_local_services()
        
        # Make the store sortable
        self.local_service_store.set_sort_func(0, self.sort_by_name)
        self.local_service_store.set_sort_func(1, self.sort_by_status)
        
        return local_box

    def create_action_button(self, label, icon_name=None):
        """Create a styled action button"""
        btn = Gtk.Button(label=label)
        btn.set_always_show_image(True)
        
        if icon_name:
            image = Gtk.Image.new_from_icon_name(icon_name, Gtk.IconSize.BUTTON)
            btn.set_image(image)
            btn.set_image_position(Gtk.PositionType.TOP)
        
        # Add button styling
        btn_style = btn.get_style_context()
        btn_style.add_class('action-button')
        
        return btn

    def escape_ansi(self, line):
        ansi_escape = re.compile(r'(?:\x1B[@-_]|[\x80-\x9F])[0-?]*[ -/]*[@-~]')
        return ansi_escape.sub('', line)

    # Get info from mostly SystemCtl, or JournalCtl cleaned of any ansi escape sequences.
    # e.g. cmd = ["systemctl", "list-unit-files", "--type=service", "--output=json", "--no-pager"]  
    def SystemGet(self, cmd):
        result = subprocess.run(cmd, capture_output=True, text=True)
        CleanedData = self.escape_ansi(result.stdout)
        return CleanedData

    def refresh_local_services(self, widget=None):
        """Refresh the list of local systemd services"""
        try:
            import json

            self.local_service_store.clear()

            # Run systemctl command with JSON output
            cmd = ["systemctl", "list-units", "--type=service", "--output=json", "--no-pager"]
            if self.show_inactive:
                cmd.append("--all")  # Show all units including inactive
            if self.show_user:
                cmd.append("--user")  # Show user units
            
            services = json.loads(self.SystemGet(cmd))

            # Run systemctl command with JSON output
            cmd = ["systemctl", "list-unit-files", "--type=service", "--output=json", "--no-pager"]
            if self.show_inactive:
                cmd.append("--all")  # Show all units including inactive
            if self.show_user:
                cmd.append("--user")  # Show user units
 
            servicefiles = json.loads(self.SystemGet(cmd))
            
            # Add service files to the store
            for service in servicefiles:
                service_name = service.get("unit_file", "")
                active_state = service.get("state", "")
                preset = service.get("preset", "")  # Get the sub-state (running, dead, etc.)
                
                # Combine active state and sub-state
                #status = f"{active_state} ({preset})"
                
                if service_name.endswith(".service"):
                    self.local_service_store.append([service_name, preset, "unit-file"])

            # Add services to the store
            for service in services:
                service_name = service.get("unit", "")
                active_state = service.get("active", "")
                sub_state = service.get("sub", "")  # Get the sub-state (running, dead, etc.)
                description = service.get("description", "")
                
                # Combine active state and sub-state
                status = f"{active_state} ({sub_state})"
                
                if service_name.endswith(".service"):
                    self.local_service_store.append([service_name, status, description])
                    
        except json.JSONDecodeError as e:
            self.show_error_dialog(f"Failed to parse service list: {str(e)}")
        except Exception as e:
            self.show_error_dialog(f"Failed to refresh local services: {str(e)}")

    def show_add_host_dialog(self, button):
        dialog = Gtk.Dialog(
            title="Add Remote Host",
            parent=self,
            flags=0
        )
        
        dialog.add_buttons(
                Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                Gtk.STOCK_OK, Gtk.ResponseType.OK
        )
        
        content = dialog.get_content_area()
        grid = Gtk.Grid()
        grid.set_column_spacing(12)
        grid.set_row_spacing(6)
        
        # Input fields
        name_entry = Gtk.Entry()
        host_entry = Gtk.Entry()
        username_entry = Gtk.Entry()
        password_entry = Gtk.Entry()
        password_entry.set_visibility(False)  # Hide password characters
        
        auth_combo = Gtk.ComboBoxText()
        auth_combo.append_text("Password")
        auth_combo.append_text("SSH Key")
        auth_combo.set_active(0)
        
        key_chooser = Gtk.FileChooserButton(title="Select SSH Key")
        key_chooser.set_sensitive(False)
        
        # Make password/key fields sensitive based on auth type
        def on_auth_changed(combo):
            is_password = combo.get_active_text() == "Password"
            password_entry.set_sensitive(is_password)
            key_chooser.set_sensitive(not is_password)
        
        auth_combo.connect("changed", on_auth_changed)
        
        # Layout
        labels = ["Name:", "Hostname:", "Username:", "Password:", "Auth Type:", "SSH Key:"]
        widgets = [name_entry, host_entry, username_entry, password_entry, auth_combo, key_chooser]
        
        for i, (label, widget) in enumerate(zip(labels, widgets)):
            grid.attach(Gtk.Label(label=label), 0, i, 1, 1)
            grid.attach(widget, 1, i, 1, 1)
        
        content.add(grid)
        dialog.show_all()
        
        response = dialog.run()
        if response == Gtk.ResponseType.OK:
            host = RemoteHost(
                name=name_entry.get_text(),
                hostname=host_entry.get_text(),
                username=username_entry.get_text(),
                auth_type="key" if auth_combo.get_active_text() == "SSH Key" else "password",
                key_path=key_chooser.get_filename() if auth_combo.get_active_text() == "SSH Key" else None
            )
            # Store password in keyring if using password auth
            if host.auth_type == "password":
                keyring.set_password(
                    "systemd-manager",
                    f"{host.username}@{host.hostname}",
                    password_entry.get_text()
            )
            self.add_remote_host(host)
        
        dialog.destroy()

    def add_remote_host(self, host: RemoteHost):
        """Add a new remote host without connecting"""
        self.remote_hosts[host.name] = host
        self.save_hosts()
        self.refresh_hosts_list()
        # No automatic connection or refresh here

    def on_connect_clicked(self, button):
        """Handle connect button click"""
        selection = self.hosts_list.get_selected_row()
        if not selection:
            return
        
        host_name = selection.get_children()[0].get_children()[1].get_text()
        host = self.remote_hosts.get(host_name)
        if not host:
            return
        
        # Show connecting dialog with spinner
        dialog = Gtk.MessageDialog(
            transient_for=self,
            flags=0,
            message_type=Gtk.MessageType.INFO,
            buttons=Gtk.ButtonsType.CANCEL,
            text=f"Connecting to {host.name}..."
        )
        
        spinner = Gtk.Spinner()
        spinner.start()
        dialog.get_content_area().pack_end(spinner, False, False, 10)
        dialog.show_all()
        
        def do_connect():
            try:
                client = paramiko.SSHClient()
                client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                
                # Connect with timeout
                client.connect(
                    hostname=host.hostname,
                    username=host.username,
                    password=keyring.get_password("systemd-manager", f"{host.username}@{host.hostname}") if host.auth_type == "password" else None,
                    key_filename=host.key_path if host.auth_type == "key" else None,
                    timeout=5  # 5 second timeout
                )
                
                # Test connection with a simple command
                stdin, stdout, stderr = client.exec_command("echo test", timeout=5)
                if stdout.read().decode().strip() != "test":
                    GLib.idle_add(lambda: self.on_connect_error("Failed to execute test command", dialog))
                    return None
                
                GLib.idle_add(lambda: self.on_connect_success(host.name, client, dialog))
                
                return client
            except Exception as e:
                self.logger.error(f"Connection failed: {str(e)}")
                return None
        
        # Run connection in a thread
        thread = threading.Thread(target=do_connect)
        thread.daemon = True
        thread.start()
        
        # Handle dialog response (Cancel button)
        response = dialog.run()
        if response == Gtk.ResponseType.CANCEL:
            dialog.destroy()

    def on_connect_success(self, host_name, client, dialog):
        """Handle successful connection"""
        self.active_connections[host_name] = client
        self.logger.info(f"Successfully connected to {host_name}")
        dialog.destroy()
        
        # Update UI
        self.refresh_hosts_list()
        # Find and select the row for this host
        for row in self.hosts_list.get_children():
            host_label = row.get_children()[0].get_children()[1]
            if host_label.get_text() == host_name:
                self.hosts_list.select_row(row)
                # Refresh services for the newly connected host
                self.refresh_services(host_name)
                break
        return False

    def on_connect_error(self, error_msg, dialog):
        """Handle connection error"""
        dialog.destroy()
        self.show_error_dialog(f"Connection failed: {error_msg}")
        return False

    def _create_ssh_client(self, host: RemoteHost) -> paramiko.SSHClient:
        """Create and configure SSH client"""
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        
        try:
            if host.auth_type == "password":
                password = keyring.get_password("systemd-manager", f"{host.username}@{host.hostname}")
                client.connect(
                    hostname=host.hostname,
                    username=host.username,
                    password=password,
                    timeout=10
                )
                
                # Configure sudo to not require tty
                client.exec_command("echo 'Defaults:$USER !requiretty' | sudo -S tee /etc/sudoers.d/systemd-manager")
                
            else:
                client.connect(
                    hostname=host.hostname,
                    username=host.username,
                    key_filename=host.key_path,
                    timeout=10
                )
                
                # Configure sudo to not require tty
                client.exec_command("echo 'Defaults:$USER !requiretty' | sudo -S tee /etc/sudoers.d/systemd-manager")
            
        except Exception as e:
            client.close()
            raise e
        
        return client

    def save_hosts(self):
        config_dir = Path.home() / ".config" / "systemd-manager"
        config_dir.mkdir(parents=True, exist_ok=True)
        
        hosts_data = {
            name: {
                "name": host.name,
                "hostname": host.hostname,
                "username": host.username,
                "auth_type": host.auth_type,
                "key_path": host.key_path
            }
            for name, host in self.remote_hosts.items()
        }
        
        with open(config_dir / "hosts.json", "w") as f:
            json.dump(hosts_data, f)

    def load_saved_hosts(self):
        config_file = Path.home() / ".config" / "systemd-manager" / "hosts.json"
        if not config_file.exists():
            return
            
        with open(config_file) as f:
            hosts_data = json.load(f)
            
        for host_data in hosts_data.values():
            host = RemoteHost(**host_data)
            self.remote_hosts[host.name] = host
            
        self.refresh_hosts_list()

    def refresh_hosts_list(self):
        """Refresh the list of hosts and their connection status"""
        for child in self.hosts_list.get_children():
            self.hosts_list.remove(child)
            
        for host in self.remote_hosts.values():
            row = Gtk.ListBoxRow()
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            
            label = Gtk.Label(label=host.name)
            status = Gtk.Image()
            
            # Update status icon based on connection state
            if host.name in self.active_connections:
                status.set_from_icon_name("gtk-yes", Gtk.IconSize.SMALL_TOOLBAR)
            else:
                status.set_from_icon_name("gtk-no", Gtk.IconSize.SMALL_TOOLBAR)
            
            box.pack_start(status, False, False, 0)
            box.pack_start(label, True, True, 0)
            row.add(box)
            self.hosts_list.add(row)
            
        self.hosts_list.show_all()

    def refresh_services(self, host_name):
        """Refresh services for a remote host"""
        client = self.active_connections.get(host_name)
        if not client:
            return False
        
        try:
            # Get services with JSON output
            cmd = "systemctl list-units --type=service --output=json --no-pager"
            if self.show_inactive:
                cmd += " --all"  # Show all units including inactive
            if self.show_user:
                cmd.append(" --user")  # Show user units
 
            stdin, stdout, stderr = client.exec_command(cmd)
            output = stdout.read().decode()
            error = stderr.read().decode()
            
            if error:
                self.logger.error(f"Failed to get services: {error}")
                return False
            
            # Parse JSON output
            import json
            services = json.loads(output)
            
            # Update service store
            self.remote_service_store.clear()
            for service in services:
                service_name = service.get("unit", "")
                active_state = service.get("active", "")
                sub_state = service.get("sub", "")  # Get the sub-state
                description = service.get("description", "")
                
                # Combine active state and sub-state
                status = f"{active_state} ({sub_state})"
                
                if service_name.endswith(".service"):
                    self.remote_service_store.append([
                        service_name,
                        status,
                        host_name,
                        description
                    ])
                    
            return False  # For GLib.timeout_add
            
        except json.JSONDecodeError as e:
            self.logger.error(f"Failed to parse service list: {str(e)}")
            return False
        except Exception as e:
            self.logger.error(f"Failed to refresh services: {str(e)}")
            return False

    def show_error_dialog(self, message: str):
        """Show an error dialog with selectable text"""
        dialog = Gtk.MessageDialog(
            transient_for=self,
            flags=0,
            message_type=Gtk.MessageType.ERROR,
            buttons=Gtk.ButtonsType.OK
        )
        dialog.set_default_size(400, -1)  # Set minimum width
        
        # Create scrolled text view
        scrolled = Gtk.ScrolledWindow()
        text_view = GtkSource.View()
        text_view.set_editable(False)
        text_view.set_cursor_visible(True)
        text_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        text_view.set_left_margin(10)
        text_view.set_right_margin(10)
        
        # Create buffer
        text_buffer = GtkSource.Buffer()
        text_view.set_buffer(text_buffer)
        text_buffer.set_text(message)
        
        scrolled.add(text_view)
        scrolled.set_min_content_height(60)
        scrolled.set_min_content_width(350)  # Set minimum content width
        
        content_area = dialog.get_content_area()
        content_area.set_margin_start(12)
        content_area.set_margin_end(12)
        content_area.set_margin_top(12)
        content_area.set_margin_bottom(12)
        content_area.add(scrolled)
        
        dialog.show_all()
        dialog.run()
        dialog.destroy()

    def on_host_button_press(self, list_box, event):
        """Handle mouse clicks on hosts"""
        # Get the row that was clicked
        row = list_box.get_row_at_y(int(event.y))
        if not row:
            return False
            
        # Single click (button 1)
        if event.type == Gdk.EventType.BUTTON_PRESS and event.button == 1:
            host_name = row.get_children()[0].get_children()[1].get_text()
            list_box.select_row(row)
            
            # If host is connected, refresh services. Otherwise just clear the list
            if host_name in self.active_connections:
                self.refresh_services(host_name)
            else:
                self.remote_service_store.clear()
            return True
        # Double click (button 1)
        elif event.type == Gdk.EventType.DOUBLE_BUTTON_PRESS and event.button == 1:
            host_name = row.get_children()[0].get_children()[1].get_text()
            host = self.remote_hosts.get(host_name)
            
            if not host:
                return True
            
            if host_name not in self.active_connections:
                # Connect when double-clicked and not connected
                self.on_connect_clicked(None)
                # After connection is established, refresh services
                if host_name in self.active_connections:
                    self.refresh_services(host_name)
            else:
                # If already connected, just refresh services
                self.refresh_services(host_name)
            return True
        
        return False

    def on_local_start_service(self, button):
        self.control_local_service("start")

    def on_local_stop_service(self, button):
        self.control_local_service("stop")

    def on_local_restart_service(self, button):
        self.control_local_service("restart")

    def control_local_service(self, action):
        """Control a local systemd service"""
        selection = self.local_service_view.get_selection()
        model, treeiter = selection.get_selected()
        if not treeiter:
            return
        
        service_name = model[treeiter][0]
        
        try:
            cmd = ["pkexec", "systemctl", action, service_name]
            subprocess.run(cmd, check=True)
            self.show_info_dialog(f"Successfully {action}ed {service_name}")
            self.refresh_local_services()
        except subprocess.CalledProcessError as e:
            self.show_error_dialog(f"Failed to {action} service: {str(e)}")

    def show_local_logs_dialog(self, button):
        """Show logs for the selected local service"""
        selection = self.local_service_view.get_selection()
        model, treeiter = selection.get_selected()
        if not treeiter:
            return
        
        service_name = model[treeiter][0]
        
        dialog = Gtk.Dialog(
            title=f"Logs - {service_name}",
            parent=self,
            flags=0
        )
        dialog.set_default_size(800, 600)
        
        scrolled, text_label = self.create_dark_text_view()
        
        try:
            # Format and set logs
            result = self.SystemGet(["journalctl", "-u", service_name, "-n", "1000", "--no-pager"])
            formatted_logs = self.format_log_output(result)
            text_label.set_markup(formatted_logs)
            
        except Exception as e:
            text_label.set_text(f"Failed to fetch logs: {str(e)}")
        
        dialog.get_content_area().pack_start(scrolled, True, True, 0)
        dialog.add_button("Close", Gtk.ResponseType.CLOSE)
        dialog.show_all()
        dialog.run()
        dialog.destroy()

    def format_log_output(self, text):
        """Format log output with Pango markup"""
        formatted_lines = []
        for line in text.splitlines():
            line = GLib.markup_escape_text(line)
            
            # Color log levels
            if "ERROR" in line.upper():
                line = line.replace(
                    "error",
                    '<span foreground="#e74c3c">error</span>'
                )
            elif "WARNING" in line.upper():
                line = line.replace(
                    "warning",
                    '<span foreground="#f1c40f">warning</span>'
                )
            elif "NOTICE" in line.upper():
                line = line.replace(
                    "notice",
                    '<span foreground="#3498db">notice</span>'
                )
            elif "INFO" in line.upper():
                line = line.replace(
                    "info",
                    '<span foreground="#2ecc71">info</span>'
                )
            
            # Highlight timestamps
            timestamp_pattern = r'([A-Z][a-z]{2} \d{2} \d{2}:\d{2}:\d{2})'
            line = re.sub(
                timestamp_pattern,
                r'<span foreground="#9b59b6">\1</span>',
                line
            )
            
            # Highlight PIDs
            pid_pattern = r'\[(\d+)\]'
            line = re.sub(
                pid_pattern,
                r'[<span foreground="#e67e22">\1</span>]',
                line
            )
                
            formatted_lines.append(line)
        
        return '<span font_family="monospace">' + '\n'.join(formatted_lines) + '</span>'

    def show_edit_host_dialog(self, button):
        selection = self.hosts_list.get_selected_row()
        if not selection:
            self.show_error_dialog("Please select a host to edit")
            return
        
        host_name = selection.get_children()[0].get_children()[1].get_text()
        host = self.remote_hosts.get(host_name)
        
        if not host:
            self.show_error_dialog("Selected host not found")
            return
        
        dialog = Gtk.Dialog(
            title="Edit Remote Host",
            parent=self,
            flags=0
        )
        
        dialog.add_buttons(
            Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
            Gtk.STOCK_OK, Gtk.ResponseType.OK
        )
        
        content = dialog.get_content_area()
        grid = Gtk.Grid()
        grid.set_column_spacing(12)
        grid.set_row_spacing(6)
        
        # Input fields
        name_entry = Gtk.Entry()
        name_entry.set_text(host.name)
        host_entry = Gtk.Entry()
        host_entry.set_text(host.hostname)
        username_entry = Gtk.Entry()
        username_entry.set_text(host.username)
        password_entry = Gtk.Entry()
        password_entry.set_visibility(False)
        
        auth_combo = Gtk.ComboBoxText()
        auth_combo.append_text("Password")
        auth_combo.append_text("SSH Key")
        auth_combo.set_active(0 if host.auth_type == "password" else 1)
        
        key_chooser = Gtk.FileChooserButton(title="Select SSH Key")
        if host.key_path:
            key_chooser.set_filename(host.key_path)
        
        # Make password/key fields sensitive based on auth type
        def on_auth_changed(combo):
            is_password = combo.get_active_text() == "Password"
            password_entry.set_sensitive(is_password)
            key_chooser.set_sensitive(not is_password)
        
        auth_combo.connect("changed", on_auth_changed)
        on_auth_changed(auth_combo)  # Set initial sensitivity
        
        # Layout
        labels = ["Name:", "Hostname:", "Username:", "Password:", "Auth Type:", "SSH Key:"]
        widgets = [name_entry, host_entry, username_entry, password_entry, auth_combo, key_chooser]
        
        for i, (label, widget) in enumerate(zip(labels, widgets)):
            grid.attach(Gtk.Label(label=label), 0, i, 1, 1)
            grid.attach(widget, 1, i, 1, 1)
        
        content.add(grid)
        dialog.show_all()
        
        response = dialog.run()
        if response == Gtk.ResponseType.OK:
            # Disconnect if connected
            if host_name in self.active_connections:
                self.active_connections[host_name].close()
                del self.active_connections[host_name]
            
            # Remove old host
            del self.remote_hosts[host_name]
            
            # Create new host
            new_host = RemoteHost(
                name=name_entry.get_text(),
                hostname=host_entry.get_text(),
                username=username_entry.get_text(),
                auth_type="key" if auth_combo.get_active_text() == "SSH Key" else "password",
                key_path=key_chooser.get_filename() if auth_combo.get_active_text() == "SSH Key" else None
            )
            
            # Update password if using password auth
            if new_host.auth_type == "password" and password_entry.get_text():
                keyring.set_password(
                    "systemd-manager",
                    f"{new_host.username}@{new_host.hostname}",
                    password_entry.get_text()
                )
            
            self.remote_hosts[new_host.name] = new_host
            self.save_hosts()
            self.refresh_hosts_list()
        
        dialog.destroy()

    def create_dark_text_view(self):
        """Create a themed text view with monospace font"""
        scrolled = Gtk.ScrolledWindow()
        label = Gtk.Label()
        label.set_selectable(True)
        label.set_line_wrap(True)
        label.set_xalign(0)  # Align text to left
        label.set_justify(Gtk.Justification.LEFT)
        scrolled.add(label)
        
        # Add theme classes
        scrolled.get_style_context().add_class('theme-background')
        label.get_style_context().add_class('theme-text')
        
        return scrolled, label

    def show_logs_dialog(self, button):
        """Show logs for the selected remote service"""
        selection = self.remote_service_view.get_selection()
        model, treeiter = selection.get_selected()
        if not treeiter:
            return
        
        service_name = model[treeiter][0]
        host_name = model[treeiter][2]
        client = self.active_connections.get(host_name)
        
        dialog = Gtk.Dialog(
            title=f"Logs - {service_name}",
            parent=self,
            flags=0
        )
        dialog.set_default_size(800, 600)
        
        scrolled, text_label = self.create_dark_text_view()
        
        try:
            stdin, stdout, stderr = client.exec_command(f"journalctl -u {service_name} -n 1000 --no-pager")
            logs = stdout.read().decode()
            
            # Format and set logs
            formatted_logs = self.format_log_output(logs)
            text_label.set_markup(formatted_logs)
            
        except Exception as e:
            text_label.set_text(f"Failed to fetch logs: {str(e)}")
        
        dialog.get_content_area().pack_start(scrolled, True, True, 0)
        dialog.add_button("Close", Gtk.ResponseType.CLOSE)
        dialog.show_all()
        dialog.run()
        dialog.destroy()

    def show_local_logs_dialog(self, button):
        """Show logs for the selected local service"""
        selection = self.local_service_view.get_selection()
        model, treeiter = selection.get_selected()
        if not treeiter:
            return
        
        service_name = model[treeiter][0]
        
        dialog = Gtk.Dialog(
            title=f"Logs - {service_name}",
            parent=self,
            flags=0
        )
        dialog.set_default_size(800, 600)
        
        scrolled, text_label = self.create_dark_text_view()
        
        try:
            # Format and set logs
            result = self.SystemGet(["journalctl", "-u", service_name, "-n", "1000", "--no-pager"])
            formatted_logs = self.format_log_output(result)
            text_label.set_markup(formatted_logs)
            
        except Exception as e:
            text_label.set_text(f"Failed to fetch logs: {str(e)}")
        
        dialog.get_content_area().pack_start(scrolled, True, True, 0)
        dialog.add_button("Close", Gtk.ResponseType.CLOSE)
        dialog.show_all()
        dialog.run()
        dialog.destroy()

    def format_properties(self, text):
        """Format systemd properties with Pango markup"""
        formatted_lines = []
        for line in text.splitlines():
            if '=' in line:
                key, value = line.split('=', 1)
                key = GLib.markup_escape_text(key)
                value = GLib.markup_escape_text(value)
                
                # Color code different types of values
                if value.lower() in ['yes', 'true', 'active', 'running', 'enabled']:
                    value = f'<span foreground="#2ecc71">{value}</span>'  # Green
                elif value.lower() in ['no', 'false', 'inactive', 'dead', 'disabled', 'failed']:
                    value = f'<span foreground="#e74c3c">{value}</span>'  # Red
                elif value.startswith('/'):
                    value = f'<span foreground="#3498db">{value}</span>'  # Blue for paths
                elif value.isdigit():
                    value = f'<span foreground="#e67e22">{value}</span>'  # Orange for numbers
                elif '@' in value or '.' in value:  # Likely an email or domain
                    value = f'<span foreground="#9b59b6">{value}</span>'  # Purple
                
                # Key is always colored consistently
                formatted_lines.append(
                    f'<span foreground="#f1c40f">{key}</span>={value}'
                )
        
        return '<span font_family="monospace">' + '\n'.join(formatted_lines) + '</span>'

    def on_service_activated(self, tree_view, path, column):
        """Handle double-click on remote service"""
        model = tree_view.get_model()
        service_name = model[path][0]
        host_name = model[path][2]
        self.show_service_details(service_name, is_remote=True, host_name=host_name)

    def on_service_button_press(self, tree_view, event):
        """Handle right-click for context menu"""
        if event.button == 3:  # Right click
            path = tree_view.get_path_at_pos(int(event.x), int(event.y))
            if path is None:
                return False
            
            tree_view.get_selection().select_path(path[0])
            
            menu = Gtk.Menu()
            items = [
                ("Start", self.on_start_service),
                ("Stop", self.on_stop_service),
                ("Restart", self.on_restart_service),
                ("Enable", self.on_enable_service),
                ("Disable", self.on_disable_service),
                ("View Logs", self.show_logs_dialog),
                ("View Details", lambda _: self.show_service_details(
                    self.remote_service_store[path[0]][0],
                    self.remote_service_store[path[0]][2]
                ))
            ]
            
            for label, callback in items:
                item = Gtk.MenuItem(label=label)
                item.connect("activate", callback)
                menu.append(item)
            
            menu.show_all()
            menu.popup_at_pointer(event)
            return True
        return False

    def show_service_details(self, service_name, is_remote=False, host_name=None):
        """Show detailed service information using Rich formatting"""
        dialog = Gtk.Dialog(
            title=f"Service Details - {service_name}",
            parent=self,
            flags=0
        )
        dialog.set_default_size(800, 600)
        
        # Create notebook for tabs
        notebook = Gtk.Notebook()
        notebook.set_margin_start(12)
        notebook.set_margin_end(12)
        notebook.set_margin_bottom(12)
        
        # Status tab with dark theme
        status_scroll = Gtk.ScrolledWindow()
        status_label = Gtk.Label()  # Use Label instead of TextView
        status_label.set_selectable(True)  # Make text selectable
        status_label.set_line_wrap(True)
        status_label.set_xalign(0)  # Align text to left
        status_scroll.add(status_label)
        notebook.append_page(status_scroll, Gtk.Label(label="Status"))
        
        # Properties tab
        props_scroll, props_label = self.create_dark_text_view()
        notebook.append_page(props_scroll, Gtk.Label(label="Properties"))
        
        # Create header box for status labels
        header_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        header_box.set_margin_start(12)
        header_box.set_margin_end(12)
        header_box.set_margin_top(12)
        header_box.set_margin_bottom(12)
        
        try:
            if is_remote:
                client = self.active_connections.get(host_name)
                if not client:
                    raise Exception("Not connected to host")
                
                # Get enabled status
                stdin, stdout, stderr = client.exec_command(f"systemctl is-enabled {service_name}")
                enabled_status = stdout.read().decode().strip()
                
                # Get active status
                stdin, stdout, stderr = client.exec_command(f"systemctl is-active {service_name}")
                active_status = stdout.read().decode().strip()
                
                # Get status output
                stdin, stdout, stderr = client.exec_command(f"systemctl status {service_name}")
                status_text = stdout.read().decode()
                
                # Apply color formatting and set markup
                formatted_text = self.format_status_output(status_text)
                status_label.set_markup(formatted_text)
                
                # Get properties
                stdin, stdout, stderr = client.exec_command(f"systemctl show {service_name}")
                details = stdout.read().decode()
                
                # Format and set properties text
                formatted_props = self.format_properties(details)
                props_label.set_markup(formatted_props)
                
            else:
                # Get local service status
                enabled_status = self.SystemGet(["systemctl", "is-enabled", service_name]).strip()
                
                active_status = self.SystemGet(["systemctl", "is-active", service_name]).strip()

                # Get local status output
                result = self.SystemGet(["systemctl", "status", service_name])
                # Apply color formatting and set markup
                formatted_text = self.format_status_output(result)
                status_label.set_markup(formatted_text)
                
                # Get local properties
                details = self.SystemGet(["systemctl", "show", service_name])
                
                # Format and set properties text
                formatted_props = self.format_properties(details)
                props_label.set_markup(formatted_props)
                
            # Create status labels with colored backgrounds
            enabled_label = Gtk.Label()
            enabled_label.set_markup(
                f'<span background="{self.get_status_color(enabled_status)}" foreground="white">'
                f' {enabled_status.upper()} </span>'
            )
            
            active_label = Gtk.Label()
            active_label.set_markup(
                f'<span background="{self.get_status_color(active_status)}" foreground="white">'
                f' {active_status.upper()} </span>'
            )
            
            header_box.pack_start(Gtk.Label(label="Status:"), False, False, 0)
            header_box.pack_start(active_label, False, False, 0)
            header_box.pack_start(Gtk.Label(label="Boot Status:"), False, False, 0)
            header_box.pack_start(enabled_label, False, False, 0)
            
        except Exception as e:
            self.logger.error(f"Failed to get service status: {str(e)}")
            header_box.pack_start(Gtk.Label(label=f"Error: {str(e)}"), True, True, 0)
            status_label.set_text(f"Failed to get service details: {str(e)}")
            props_label.set_text(f"Failed to get service properties: {str(e)}")
        
        # Add dark theme CSS
        css_provider = Gtk.CssProvider()
        css_provider.load_from_data(b"""
            scrolledwindow {
                background-color: #1c1c1c;
            }
            label {
                color: #ffffff;
                background-color: #1c1c1c;
                padding: 10px;
            }
        """)
        
        status_scroll.get_style_context().add_provider(
            css_provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        status_label.get_style_context().add_provider(
            css_provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        
        # Add header and notebook to dialog
        content_area = dialog.get_content_area()
        content_area.pack_start(header_box, False, False, 0)
        content_area.pack_start(notebook, True, True, 0)
        
        dialog.add_button("Close", Gtk.ResponseType.CLOSE)
        dialog.show_all()
        dialog.run()
        dialog.destroy()

    def get_status_color(self, status):
        """Get background color for status label"""
        status = status.lower()
        if status in ['active', 'running', 'enabled']:
            return "#2ecc71"  # Green
        elif status in ['inactive', 'dead', 'disabled']:
            return "#e74c3c"  # Red
        elif status in ['activating', 'deactivating']:
            return "#f1c40f"  # Yellow
        else:
            return "#95a5a6"  # Gray

    def on_local_service_activated(self, tree_view, path, column):
        """Handle double-click on local service"""
        model = tree_view.get_model()
        service_name = model[path][0]
        self.show_service_details(service_name, is_remote=False)

    def on_local_service_button_press(self, tree_view, event):
        """Handle right-click for context menu on local services"""
        if event.button == 3:  # Right click
            path = tree_view.get_path_at_pos(int(event.x), int(event.y))
            if path is None:
                return False
            
            tree_view.get_selection().select_path(path[0])
            
            menu = Gtk.Menu()
            items = [
                ("Start", self.on_local_start_service),
                ("Stop", self.on_local_stop_service),
                ("Restart", self.on_local_restart_service),
                ("Enable", self.on_local_enable_service),
                ("Disable", self.on_local_disable_service),
                ("View Logs", self.show_local_logs_dialog),
                ("View Details", lambda _: self.show_local_service_details(
                    self.local_service_store[path[0]][0]
                ))
            ]
            
            for label, callback in items:
                item = Gtk.MenuItem(label=label)
                item.connect("activate", callback)
                menu.append(item)
            
            menu.show_all()
            menu.popup_at_pointer(event)
            return True
        return False

    def show_local_service_details(self, service_name):
        """Show detailed information for local service"""
        dialog = Gtk.Dialog(
            title=f"Service Details - {service_name.replace('.service', '')}",
            parent=self,
            flags=0
        )
        dialog.add_buttons(Gtk.STOCK_CLOSE, Gtk.ResponseType.CLOSE)
        dialog.set_default_size(600, 400)
        
        content = dialog.get_content_area()
        
        # Add status header
        header_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        header_box.set_margin_start(12)
        header_box.set_margin_end(12)
        header_box.set_margin_top(12)
        header_box.set_margin_bottom(12)
        
        try:
            # Get enabled status
            enabled_status = self.SystemGet(["systemctl", "is-enabled", service_name]).strip()

            # Get active status
            active_status = self.SystemGet(["systemctl", "is-active", service_name]).strip()

            # Create status labels with colored backgrounds
            enabled_label = Gtk.Label()
            enabled_label.set_markup(
                f'<span background="{self.get_status_color(enabled_status)}" foreground="white">'
                f' {enabled_status.upper()} </span>'
            )
            
            active_label = Gtk.Label()
            active_label.set_markup(
                f'<span background="{self.get_status_color(active_status)}" foreground="white">'
                f' {active_status.upper()} </span>'
            )
            
            header_box.pack_start(Gtk.Label(label="Status:"), False, False, 0)
            header_box.pack_start(active_label, False, False, 0)
            header_box.pack_start(Gtk.Label(label="Boot Status:"), False, False, 0)
            header_box.pack_start(enabled_label, False, False, 0)
            
        except Exception as e:
            self.logger.error(f"Failed to get service status: {str(e)}")
        
        content.pack_start(header_box, False, False, 0)
        
        # Add notebook with details
        notebook = Gtk.Notebook()
        
        # Status page
        status_view = Gtk.TextView()
        status_scroll = Gtk.ScrolledWindow()
        status_scroll.add(status_view)
        notebook.append_page(status_scroll, Gtk.Label(label="Status"))
        
        # Properties page
        props_view = Gtk.TextView()
        props_scroll = Gtk.ScrolledWindow()
        props_scroll.add(props_view)
        notebook.append_page(props_scroll, Gtk.Label(label="Properties"))
        
        content.pack_start(notebook, True, True, 0)
        
        try:
            # Get status
            status_view.get_buffer().set_text(self.SystemGet(["systemctl", "status", service_name]))
            
            # Get properties
            props_view.get_buffer().set_text(self.SystemGet(["systemctl", "show", service_name]))
            
        except Exception as e:
            self.show_error_dialog(f"Failed to fetch service details: {str(e)}")
        
        dialog.show_all()
        dialog.run()
        dialog.destroy()

    def format_service_cell(self, column, cell, model, iter, data):
        """Format service name and description in the cell"""
        try:
            name = model.get_value(iter, 0)
            description = model.get_value(iter, 3)  # Description is in column 3
            
            if description:
                markup = f"<b>{GLib.markup_escape_text(name)}</b>\n<small>{GLib.markup_escape_text(description)}</small>"
            else:
                markup = f"<b>{GLib.markup_escape_text(name)}</b>"
                
            cell.set_property("markup", markup)
            
        except Exception as e:
            self.logger.error(f"Error formatting service cell: {str(e)}")
            cell.set_property("text", model.get_value(iter, 0))

    def format_local_service_cell(self, column, cell, model, iter, data):
        """Format service name and description in a single cell for local services"""
        name = model[iter][0].replace(".service", "")
        description = model[iter][2]
        
        if description:
            cell.set_property("markup", 
                f'<b>{name}</b>\n<span size="smaller" style="italic">{description}</span>')
        else:
            cell.set_property("markup", f'<b>{name}</b>')

    def on_selection_changed(self, list_box):
        """Handle selection changes in the hosts list"""
        # Only clear the service list
        self.remote_service_store.clear()

    def on_start_service(self, button):
        self.control_service("start")

    def on_stop_service(self, button):
        self.control_service("stop")

    def on_restart_service(self, button):
        self.control_service("restart")

    def control_service(self, action):
        """Control a remote systemd service"""
        selection = self.remote_service_view.get_selection()
        model, treeiter = selection.get_selected()
        if not treeiter:
            return
            
        service_name = model[treeiter][0]
        host_name = model[treeiter][2]
        
        client = self.active_connections.get(host_name)
        if not client:
            self.show_error_dialog("Host not connected")
            return
            
        try:
            success, sudo_password = self.show_sudo_password_dialog(
                host=host_name,
                command=f"systemctl {action} {service_name}"
            )
            
            if not success:
                return
            
            cmd = f"echo '{sudo_password}' | sudo -S systemctl {action} {service_name}"
            stdin, stdout, stderr = client.exec_command(cmd)
            error = stderr.read().decode()
            
            if error and not "password" in error.lower():
                self.show_error_dialog(f"Failed to {action} service: {error}")
            else:
                self.show_info_dialog(f"Successfully {action}ed {service_name}")
                self.refresh_services(host_name)
                
        except Exception as e:
            self.show_error_dialog(f"Failed to {action} service: {str(e)}")

    def show_logs_dialog(self, button):
        """Show logs for the selected remote service"""
        selection = self.remote_service_view.get_selection()
        model, treeiter = selection.get_selected()
        if not treeiter:
            return
            
        service_name = model[treeiter][0]
        host_name = model[treeiter][2]
        client = self.active_connections.get(host_name)
            
        dialog = Gtk.Dialog(
            title=f"Logs - {service_name}",
            parent=self,
            flags=0
        )
        dialog.set_default_size(800, 600)
        
        scrolled, text_label = self.create_dark_text_view()
        
        try:
            stdin, stdout, stderr = client.exec_command(f"journalctl -u {service_name} -n 1000 --no-pager")
            logs = stdout.read().decode()
            
            # Format and set logs
            formatted_logs = self.format_log_output(logs)
            text_label.set_markup(formatted_logs)
            
        except Exception as e:
            text_label.set_text(f"Failed to fetch logs: {str(e)}")
        
        dialog.get_content_area().pack_start(scrolled, True, True, 0)
        dialog.add_button("Close", Gtk.ResponseType.CLOSE)
        dialog.show_all()
        dialog.run()
        dialog.destroy()

    def show_info_dialog(self, message: str):
        """Show an information dialog with selectable text"""
        dialog = Gtk.MessageDialog(
            transient_for=self,
            flags=0,
            message_type=Gtk.MessageType.INFO,
            buttons=Gtk.ButtonsType.OK
        )
        dialog.set_default_size(400, -1)  # Set minimum width
        
        # Create scrolled text view
        scrolled = Gtk.ScrolledWindow()
        text_view = GtkSource.View()
        text_view.set_editable(False)
        text_view.set_cursor_visible(True)
        text_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        text_view.set_left_margin(10)
        text_view.set_right_margin(10)
        
        # Create buffer
        text_buffer = GtkSource.Buffer()
        text_view.set_buffer(text_buffer)
        text_buffer.set_text(message)
        
        scrolled.add(text_view)
        scrolled.set_min_content_height(60)
        scrolled.set_min_content_width(350)  # Set minimum content width
        
        content_area = dialog.get_content_area()
        content_area.set_margin_start(12)
        content_area.set_margin_end(12)
        content_area.set_margin_top(12)
        content_area.set_margin_bottom(12)
        content_area.add(scrolled)
        
        dialog.show_all()
        dialog.run()
        dialog.destroy()

    def show_error_dialog(self, message: str):
        """Show an error dialog with selectable text"""
        dialog = Gtk.MessageDialog(
            transient_for=self,
            flags=0,
            message_type=Gtk.MessageType.ERROR,
            buttons=Gtk.ButtonsType.OK
        )
        dialog.set_default_size(400, -1)  # Set minimum width
        
        # Create scrolled text view
        scrolled = Gtk.ScrolledWindow()
        text_view = GtkSource.View()
        text_view.set_editable(False)
        text_view.set_cursor_visible(True)
        text_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        text_view.set_left_margin(10)
        text_view.set_right_margin(10)
        
        # Create buffer
        text_buffer = GtkSource.Buffer()
        text_view.set_buffer(text_buffer)
        text_buffer.set_text(message)
        
        scrolled.add(text_view)
        scrolled.set_min_content_height(60)
        scrolled.set_min_content_width(350)  # Set minimum content width
        
        content_area = dialog.get_content_area()
        content_area.set_margin_start(12)
        content_area.set_margin_end(12)
        content_area.set_margin_top(12)
        content_area.set_margin_bottom(12)
        content_area.add(scrolled)
        
        dialog.show_all()
        dialog.run()
        dialog.destroy()

    def on_enable_service(self, button):
        self.control_service("enable")

    def on_disable_service(self, button):
        self.control_service("disable")

    def on_local_enable_service(self, button):
        self.control_local_service("enable")

    def on_local_disable_service(self, button):
        self.control_local_service("disable")

    def show_sudo_password_dialog(self, host=None, command=None):
        """Show an improved sudo password dialog with context
        
        Args:
            host: Optional[str] - Remote hostname if applicable
            command: Optional[str] - Command that requires sudo
            
        Returns:
            tuple(bool, str) - (Success, Password)
        """
        password_dialog = Gtk.MessageDialog(
            transient_for=self,
            flags=0,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.OK_CANCEL,
            text="Sudo Authentication Required"
        )
        
        # Create content box
        content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        content_box.set_margin_start(12)
        content_box.set_margin_end(12)
        content_box.set_margin_top(12)
        content_box.set_margin_bottom(12)
        
        # Add context information
        context_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        
        if host:
            host_label = Gtk.Label()
            host_label.set_markup(f"<b>Host:</b> {host}")
            host_label.set_halign(Gtk.Align.START)
            context_box.pack_start(host_label, False, False, 0)
        
        user_label = Gtk.Label()
        if host:
            user = self.remote_hosts[host].username
        else:
            import os
            user = os.getenv('USER')
        user_label.set_markup(f"<b>User:</b> {user}")
        user_label.set_halign(Gtk.Align.START)
        context_box.pack_start(user_label, False, False, 0)
        
        if command:
            cmd_label = Gtk.Label()
            cmd_label.set_markup(f"<b>Command:</b> {command}")
            cmd_label.set_halign(Gtk.Align.START)
            cmd_label.set_line_wrap(True)
            context_box.pack_start(cmd_label, False, False, 0)
        
        content_box.pack_start(context_box, False, False, 0)
        
        # Add password entry
        entry_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        password_label = Gtk.Label(label="Password:")
        password_entry = Gtk.Entry()
        password_entry.set_visibility(False)
        password_entry.set_placeholder_text("Enter sudo password")
        
        # Add Enter key handling
        def on_password_entry_activate(entry):
            password_dialog.response(Gtk.ResponseType.OK)
        password_entry.connect("activate", on_password_entry_activate)
        
        entry_box.pack_start(password_label, False, False, 0)
        entry_box.pack_start(password_entry, True, True, 0)
        content_box.pack_start(entry_box, False, False, 0)
        
        password_dialog.get_content_area().add(content_box)
        password_dialog.show_all()
        password_entry.grab_focus()  # Give focus to password entry
        
        response = password_dialog.run()
        password = password_entry.get_text()
        password_dialog.destroy()
        
        return response == Gtk.ResponseType.OK, password

    def reload_systemd_config(self, button=None):
        """Reload systemd configuration"""
        is_remote = self.stack.get_visible_child_name() == "remote"
        
        if is_remote:
            # Get selected remote host
            selection = self.hosts_list.get_selected_row()
            if not selection:
                self.show_error_dialog("Please select a remote host")
                return
            
            host_name = selection.get_children()[0].get_children()[1].get_text()
            client = self.active_connections.get(host_name)
            if not client:
                self.show_error_dialog("Please connect to the remote host first")
                return
            
            try:
                success, sudo_password = self.show_sudo_password_dialog(
                    host=host_name,
                    command="systemctl daemon-reload"
                )
            except Exception as e:
                self.show_error_dialog(f"Failed to show password dialog: {str(e)}")
                return
                
                if not success:
                    return
                    
                # Execute remote reload
                cmd = f"echo '{sudo_password}' | sudo -S systemctl daemon-reload"
                stdin, stdout, stderr = client.exec_command(cmd)
                error = stderr.read().decode()
                
                if error and not "password" in error.lower():
                    self.show_error_dialog(f"Failed to reload configuration: {error}")
                else:
                    self.show_info_dialog("Systemd configuration reloaded")
                    self.refresh_services(host_name)
            except Exception as e:
                self.show_error_dialog(f"Failed to reload configuration: {str(e)}")
        else:
            # Local reload
            try:
                subprocess.run(["pkexec", "systemctl", "daemon-reload"], check=True)
                self.show_info_dialog("Systemd configuration reloaded")
                self.refresh_local_services()
            except subprocess.CalledProcessError as e:
                self.show_error_dialog(f"Failed to reload configuration: {str(e)}")

    def show_create_service_dialog(self, button):
        """Show dialog to create a new systemd service"""
        # Check if we're on the remote page and a host is connected
        is_remote = self.stack.get_visible_child_name() == "remote"
        if is_remote:
            selection = self.hosts_list.get_selected_row()
            if not selection:
                self.show_error_dialog("Please select a remote host")
                return
            
            host_name = selection.get_children()[0].get_children()[1].get_text()
            client = self.active_connections.get(host_name)
            if not client:
                self.show_error_dialog("Please connect to the remote host first")
                return
        
        # Create dialog
        dialog = Gtk.Dialog(
            title="Create Service",
            parent=self,
            flags=0
        )
        dialog.add_buttons(
            Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
            Gtk.STOCK_OK, Gtk.ResponseType.OK
        )
        dialog.set_default_size(700, 500)
        
        content = dialog.get_content_area()
        content.set_spacing(6)
        
        # Service name field at the top
        name_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        name_box.set_margin_start(12)
        name_box.set_margin_end(12)
        name_box.set_margin_top(12)
        
        name_label = Gtk.Label(label="Service Name:")
        name_entry = Gtk.Entry()
        name_entry.set_placeholder_text("my-service")
        
        name_box.pack_start(name_label, False, False, 0)
        name_box.pack_start(name_entry, True, True, 0)
        
        content.add(name_box)
        
        # Create source view for editing
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_margin_start(12)
        scrolled.set_margin_end(12)
        scrolled.set_margin_bottom(12)
        
        # Set up the source view
        source_buffer = GtkSource.Buffer()
        source_buffer.set_language(
            GtkSource.LanguageManager.get_default().get_language('ini')
        )
        source_buffer.set_style_scheme(
            GtkSource.StyleSchemeManager.get_default().get_scheme('oblivion')
        )
        
        source_view = GtkSource.View.new_with_buffer(source_buffer)
        source_view.set_show_line_numbers(True)
        source_view.set_auto_indent(True)
        source_view.set_tab_width(4)
        source_view.set_indent_width(4)
        source_view.set_insert_spaces_instead_of_tabs(True)
        source_view.set_smart_backspace(True)
        
        # Set default template
        template = """[Unit]
Description=My custom service
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/sleep infinity

[Install]
WantedBy=multi-user.target
"""
        source_buffer.set_text(template)
        
        scrolled.add(source_view)
        content.pack_start(scrolled, True, True, 0)
        
        dialog.show_all()
        
        while True:
            response = dialog.run()
            if response == Gtk.ResponseType.OK:
                service_name = name_entry.get_text()
                if not service_name:
                    self.show_error_dialog("Please enter a service name")
                    continue
                    
                if not service_name.endswith('.service'):
                    service_name += '.service'
                
                service_content = source_buffer.get_text(
                    source_buffer.get_start_iter(),
                    source_buffer.get_end_iter(),
                    True
                )
                
                try:
                    if is_remote:
                        self.logger.info(f"Creating remote service {service_name}")
                        
                        # Check if we're root first
                        stdin, stdout, stderr = client.exec_command("id -u")
                        uid = stdout.read().decode().strip()
                        is_root = (uid == "0")
                        
                        if not is_root:
                            # Use the existing better sudo dialog
                            success, sudo_password = self.show_sudo_password_dialog(
                                host=host_name,
                                command=f"Create and configure service: {service_name}"
                            )
                            if not success:
                                continue
                            
                            # Function to run sudo command with saved password
                            def run_sudo_command(cmd):
                                full_cmd = f"echo '{sudo_password}' | sudo -S {cmd}"
                                stdin, stdout, stderr = client.exec_command(full_cmd)
                                error = stderr.read().decode()
                                output = stdout.read().decode()
                                return output, error
                        else:
                            # We're root, no need for sudo
                            def run_sudo_command(cmd):
                                stdin, stdout, stderr = client.exec_command(cmd)
                                error = stderr.read().decode()
                                output = stdout.read().decode()
                                return output, error
                        
                        # Create service file and write content
                        self.logger.debug("Creating service file and writing content")
                        temp_file = f"/tmp/{service_name}"
                        cmd = f"echo '{service_content}' > {temp_file}"
                        stdin, stdout, stderr = client.exec_command(cmd)
                        error = stderr.read().decode()
                        
                        if error:
                            self.logger.error(f"Failed to create temp file: {error}")
                            self.show_error_dialog(f"Failed to create temp file: {error}")
                            continue
                            
                        # Move file to systemd directory
                        cmd = f"mv {temp_file} /etc/systemd/system/" if is_root else f"echo '{sudo_password}' | sudo -S mv {temp_file} /etc/systemd/system/"
                        stdin, stdout, stderr = client.exec_command(cmd)
                        error = stderr.read().decode()
                        
                        if error and (not is_root and not "password" in error.lower()):
                            self.logger.error(f"Failed to move service file: {error}")
                            self.show_error_dialog(f"Failed to move service file: {error}")
                            continue
                            
                        # Set proper permissions
                        self.logger.debug("Setting file permissions")
                        cmd = f"chmod 644 /etc/systemd/system/{service_name}" if is_root else f"echo '{sudo_password}' | sudo -S chmod 644 /etc/systemd/system/{service_name}"
                        stdin, stdout, stderr = client.exec_command(cmd)
                        error = stderr.read().decode()
                        
                        if error and (not is_root and not "password" in error.lower()):
                            self.logger.error(f"Failed to set permissions: {error}")
                            self.show_error_dialog(f"Failed to set permissions: {error}")
                            continue
                        
                        # Verify the file exists
                        cmd = f"test -f /etc/systemd/system/{service_name} && echo 'exists'"
                        stdin, stdout, stderr = client.exec_command(cmd)
                        output = stdout.read().decode().strip()
                        
                        if output != 'exists':
                            self.logger.error("Service file was not created")
                            self.show_error_dialog("Failed to create service file")
                            continue
                        
                        # Reload systemd
                        self.logger.debug("Reloading systemd")
                        cmd = f"echo '{sudo_password}' | sudo -S systemctl daemon-reload"
                        stdin, stdout, stderr = client.exec_command(cmd)
                        error = stderr.read().decode()
                        
                        if error and not "password" in error.lower():
                            self.logger.error(f"Failed to reload systemd: {error}")
                            self.show_error_dialog(f"Failed to reload systemd: {error}")
                            continue
                            
                    else:
                        # Get sudo password once for all local operations
                        success, sudo_password = self.show_sudo_password_dialog(
                            command=f"Create and configure service: {service_name}"
                        )
                        if not success:
                            continue
                        
                        # Helper function for local sudo commands
                        def run_local_sudo_command(cmd, input_data=None):
                            process = subprocess.Popen(
                                ["sudo", "-S"] + cmd,
                                stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,
                                text=True
                            )
                            
                            if input_data:
                                stdout, stderr = process.communicate(f"{sudo_password}\n{input_data}")
                            else:
                                stdout, stderr = process.communicate(sudo_password + "\n")
                            
                            return process.returncode, stdout, stderr
                        
                        # Create service file
                        returncode, stdout, stderr = run_local_sudo_command(
                            ["tee", f"/etc/systemd/system/{service_name}"],
                            service_content
                        )
                        
                        if returncode != 0:
                            self.show_error_dialog(f"Failed to create service file: {stderr}")
                            continue
                        
                        # Set proper permissions
                        returncode, stdout, stderr = run_local_sudo_command(
                            ["chmod", "644", f"/etc/systemd/system/{service_name}"]
                        )
                        
                        if returncode != 0:
                            self.show_error_dialog(f"Failed to set permissions: {stderr}")
                            continue
                        
                        # Reload systemd
                        returncode, stdout, stderr = run_local_sudo_command(
                            ["systemctl", "daemon-reload"]
                        )
                        
                        if returncode != 0:
                            self.show_error_dialog(f"Failed to reload systemd: {stderr}")
                            continue
                    
                    # Ask if the service should be started (moved outside the if/else)
                    start_dialog = Gtk.MessageDialog(
                        transient_for=self,
                        flags=0,
                        message_type=Gtk.MessageType.QUESTION,
                        buttons=Gtk.ButtonsType.YES_NO,
                        text="Service Created"
                    )
                    start_dialog.format_secondary_text(
                        f"Service {service_name} was created successfully. Would you like to start it now?"
                    )
                    
                    start_response = start_dialog.run()
                    start_dialog.destroy()
                    
                    if start_response == Gtk.ResponseType.YES:
                        if is_remote:
                            output, error = run_sudo_command(f"systemctl start {service_name}")
                            if error and not "password" in error.lower():
                                self.logger.error(f"Failed to start service: {error}")
                                self.show_error_dialog(f"Failed to start service: {error}")
                            else:
                                self.show_info_dialog(f"Service {service_name} started successfully")
                        else:
                            returncode, stdout, stderr = run_local_sudo_command(
                                ["systemctl", "start", service_name]
                            )
                            if returncode == 0:
                                self.show_info_dialog(f"Service {service_name} started successfully")
                            else:
                                self.show_error_dialog(f"Failed to start service: {stderr}")
                    
                    # Refresh the appropriate service list
                    if is_remote:
                        GLib.timeout_add(1000, lambda: self.refresh_services(host_name))
                    else:
                        GLib.timeout_add(1000, self.refresh_local_services)
                    
                    break
                    
                except Exception as e:
                    self.show_error_dialog(f"Failed to create service: {str(e)}")
                    continue
            else:
                break
        
        dialog.destroy()

    def sort_by_name(self, model, iter1, iter2, user_data):
        """Sort services by name"""
        name1 = model.get_value(iter1, 0)
        name2 = model.get_value(iter2, 0)
        return (name1 > name2) - (name1 < name2)

    def sort_by_status(self, model, iter1, iter2, user_data):
        """Sort services by status"""
        status1 = model.get_value(iter1, 1)
        status2 = model.get_value(iter2, 1)
        return (status1 > status2) - (status1 < status2)

    def sort_by_host(self, model, iter1, iter2, user_data):
        """Sort services by host"""
        host1 = model.get_value(iter1, 2)
        host2 = model.get_value(iter2, 2)
        return (host1 > host2) - (host1 < host2)

    def format_status_output(self, text):
        """Format systemctl status output with Pango markup"""
        formatted_lines = []
        for line in text.splitlines():
            # Escape any existing markup
            line = GLib.markup_escape_text(line)
            
            # Highlight service name and description
            if "Loaded:" in line:
                line = line.replace(
                    "loaded",
                    '<span foreground="#2ecc71">loaded</span>'  # Green
                )
            elif "Active:" in line:
                if "active (running)" in line.lower():
                    line = line.replace(
                        "active",
                        '<span foreground="#2ecc71">active</span>'  # Green
                    )
                elif "inactive" in line.lower():
                    line = line.replace(
                        "inactive",
                        '<span foreground="#e74c3c">inactive</span>'  # Red
                    )
                elif "failed" in line.lower():
                    line = line.replace(
                        "failed",
                        '<span foreground="#e74c3c">failed</span>'  # Red
                    )
            elif "Main PID:" in line:
                line = f'<span foreground="#3498db">{line}</span>'  # Blue
            elif "CGroup:" in line:
                line = f'<span foreground="#f1c40f">{line}</span>'  # Yellow
            
            formatted_lines.append(line)
        
        return '<span font_family="monospace">' + '\n'.join(formatted_lines) + '</span>'

    def on_disconnect_clicked(self, button):
        """Handle disconnect button click"""
        selection = self.hosts_list.get_selected_row()
        if not selection:
            self.show_error_dialog("Please select a host to disconnect")
            return
        
        host_name = selection.get_children()[0].get_children()[1].get_text()
        client = self.active_connections.get(host_name)
        
        if not client:
            self.show_error_dialog("Host is not connected")
            return
        
        try:
            client.close()
            del self.active_connections[host_name]
            self.refresh_hosts_list()  # Update the host list to show disconnected state
            self.remote_service_store.clear()  # Clear the services list
            self.show_info_dialog(f"Disconnected from {host_name}")
            
        except Exception as e:
            self.show_error_dialog(f"Failed to disconnect: {str(e)}")

    def toggle_theme(self, button):
        """Toggle between light and dark theme"""
        self.is_dark_mode = not self.is_dark_mode
        self.settings.set_property("gtk-application-prefer-dark-theme", self.is_dark_mode)
        
        # Update CSS for custom widgets if needed
        self.update_custom_theme()

    def update_custom_theme(self):
        """Update custom widget themes based on current mode"""
        # Common CSS for both themes - static black background for text views
        css_data = b"""
            .action-bar {
                border-top: 1px solid alpha(#000000, 0.1);
                padding: 6px;
            }
            
            .action-button {
                padding: 8px;
                margin: 4px;
                border-radius: 4px;
                min-width: 80px;
            }
            
            .action-button:hover {
                background-color: alpha(#000000, 0.1);
            }
            
            /* Always black background for text content */
            .theme-text {
                color: #ffffff;
                background-color: #000000;
                padding: 10px;
                font-family: monospace;
            }
            
            .theme-background {
                background-color: #000000;
                padding: 6px;
            }
            
            textview {
                color: #ffffff;
                background-color: #000000;
                font-family: monospace;
            }
            
            textview text {
                color: #ffffff;
                background-color: #000000;
            }
            
            scrolledwindow {
                background-color: #000000;
            }
        """
        
        self.css_provider.load_from_data(css_data)

    def show_about_dialog(self, button):
        """Show the About dialog"""
        about_dialog = Gtk.AboutDialog(transient_for=self)
        about_dialog.set_modal(True)
        
        # Set dialog properties
        about_dialog.set_program_name(APP_NAME)
        about_dialog.set_version(APP_VERSION)
        about_dialog.set_comments(APP_DESCRIPTION)
        about_dialog.set_authors(APP_AUTHORS)
        about_dialog.set_website(APP_WEBSITE)
        about_dialog.set_website_label("Project website")
        about_dialog.set_license_type(Gtk.License.GPL_3_0)
        about_dialog.set_logo_icon_name(None)  # Try this instead
        about_dialog.set_logo(None)
        about_dialog.set_icon(None)
        
        about_dialog.connect("response", lambda d, r: d.destroy())
        about_dialog.show()

    def create_dark_source_view(self, language_id=None):
        """Create a dark themed source view with syntax highlighting"""
        scrolled = Gtk.ScrolledWindow()
        
        # Set up source view with better editing features
        source_buffer = GtkSource.Buffer()
        if language_id:
            source_buffer.set_language(
                GtkSource.LanguageManager.get_default().get_language(language_id)
            )
        
        # Use default style scheme for syntax highlighting
        style_manager = GtkSource.StyleSchemeManager.get_default()
        style_scheme = style_manager.get_scheme('classic')  # Use classic scheme for black on white
        source_buffer.set_style_scheme(style_scheme)
        
        source_view = GtkSource.View.new_with_buffer(source_buffer)
        source_view.set_show_line_numbers(True)
        source_view.set_auto_indent(True)
        source_view.set_indent_width(2)
        source_view.set_insert_spaces_instead_of_tabs(True)
        source_view.set_highlight_current_line(True)
        source_view.set_monospace(True)
        
        # Force black on white colors
        css_provider = Gtk.CssProvider()
        css_provider.load_from_data(b"""
            textview {
                color: #000000;
                background-color: #ffffff;
            }
            textview text {
                color: #000000;
                background-color: #ffffff;
            }
        """)
        
        source_view.get_style_context().add_provider(
            css_provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        
        scrolled.add(source_view)
        return scrolled, source_view

    def toggle_show_inactive(self, button):
        """Toggle showing inactive services"""
        self.show_inactive = not self.show_inactive
        # Update both local and remote service lists
        self.refresh_local_services()
        if self.stack.get_visible_child_name() == "remote":
            selection = self.hosts_list.get_selected_row()
            if selection:
                host_name = selection.get_children()[0].get_children()[1].get_text()
                if host_name in self.active_connections:
                    self.refresh_services(host_name)

    def toggle_show_user(self, button):
        """Toggle showing user services"""
        self.show_user = not self.show_user
        # Update both local and remote service lists
        self.refresh_local_services()
        if self.stack.get_visible_child_name() == "remote":
            selection = self.hosts_list.get_selected_row()
            if selection:
                host_name = selection.get_children()[0].get_children()[1].get_text()
                if host_name in self.active_connections:
                    self.refresh_services(host_name)

def main():
    win = SystemdManagerWindow()
    win.connect("destroy", Gtk.main_quit)
    win.show_all()
    Gtk.main()

if __name__ == "__main__":
    main()
