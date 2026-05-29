// =============================================================================
// qt-install.qs
// -----------------------------------------------------------------------------
// Qt Installer Framework script for unattended install of Qt 5.3.2.
//
// Selects ONLY the Android armv7 component (qt.53.android_armv7) and installs
// to /root/Qt/5.3.  Run under xvfb-run because the QIF GUI is required (Qt 5.3
// era has no true --silent / --headless mode).
//
// Invoked from Dockerfile.pyqt5-plashless:
//     xvfb-run --auto-servernum --server-args='-screen 0 1280x800x24' \
//         ./qt-installer.run --script /tmp/qt-install.qs --no-force-installations --verbose
//
// Reference: https://doc.qt.io/qtinstallerframework/scripting.html
// =============================================================================

function Controller() {
    installer.autoRejectMessageBoxes();

    installer.installationFinished.connect(function() {
        gui.clickButton(buttons.NextButton);
    });
}

Controller.prototype.WelcomePageCallback = function() {
    // Some installer versions delay enabling the Next button on the welcome
    // page until the network availability check completes.  Wait briefly.
    gui.clickButton(buttons.NextButton, 4000);
};

Controller.prototype.CredentialsPageCallback = function() {
    // Qt 5.3-era installers don't always show this page; if they do, skip it.
    gui.clickButton(buttons.NextButton);
};

Controller.prototype.IntroductionPageCallback = function() {
    gui.clickButton(buttons.NextButton);
};

Controller.prototype.TargetDirectoryPageCallback = function() {
    // IMPORTANT: Qt's installer ALWAYS appends a version subfolder to the
    // target directory.  Setting target to /root/Qt yields the conventional
    // layout /root/Qt/5.3/android_armv7/.  Setting it to /root/Qt/5.3 would
    // produce the doubled path /root/Qt/5.3/5.3/android_armv7/.
    gui.currentPageWidget().TargetDirectoryLineEdit.setText("/root/Qt");
    gui.clickButton(buttons.NextButton);
};

Controller.prototype.ComponentSelectionPageCallback = function() {
    var widget = gui.currentPageWidget();

    // Deselect everything Qt's installer pre-selected by default.
    widget.deselectAll();

    // Select ONLY the Android armv7 component for Qt 5.3.
    // Different installer revisions name this slightly differently; try the
    // most common identifiers in order.
    var candidates = [
        "qt.53.android_armv7",
        "qt.532.android_armv7",
        "qt.5.3.android_armv7"
    ];
    var picked = null;
    for (var i = 0; i < candidates.length; ++i) {
        try {
            widget.selectComponent(candidates[i]);
            picked = candidates[i];
            break;
        } catch (e) {
            // not this one
        }
    }
    if (picked === null) {
        // Last resort: try anything containing 'android_armv7'
        try {
            widget.selectComponent("qt.android_armv7");
        } catch (e) { /* ignore */ }
    }
    gui.clickButton(buttons.NextButton);
};

Controller.prototype.LicenseAgreementPageCallback = function() {
    gui.currentPageWidget().AcceptLicenseRadioButton.setChecked(true);
    gui.clickButton(buttons.NextButton);
};

Controller.prototype.StartMenuDirectoryPageCallback = function() {
    gui.clickButton(buttons.NextButton);
};

Controller.prototype.ReadyForInstallationPageCallback = function() {
    gui.clickButton(buttons.NextButton);
};

Controller.prototype.PerformInstallationPageCallback = function() {
    // No-op; the installer drives this page itself.
};

Controller.prototype.FinishedPageCallback = function() {
    var checkBoxForm = gui.currentPageWidget().LaunchQtCreatorCheckBoxForm;
    if (checkBoxForm && checkBoxForm.launchQtCreatorCheckBox) {
        checkBoxForm.launchQtCreatorCheckBox.checked = false;
    }
    gui.clickButton(buttons.FinishButton);
};
