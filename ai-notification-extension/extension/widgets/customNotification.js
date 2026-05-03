import GObject from 'gi://GObject';
import Clutter from 'gi://Clutter';
import St from 'gi://St';
import * as MessageTray from 'resource:///org/gnome/shell/ui/messageTray.js';
import { CountdownIndicator } from './countdownIndicator.js';

/**
 * Custom notification with vertical button support
 * Extends MessageTray.Notification to support 4-5 action buttons in vertical layout
 */
export const CustomNotification = GObject.registerClass({
    GTypeName: 'AINotificationCustomNotification',
    Properties: {
        'action-layout': GObject.ParamSpec.string(
            'action-layout',
            'Action Layout',
            'Layout style for action buttons',
            GObject.ParamFlags.READWRITE,
            'horizontal'
        ),
    },
}, class CustomNotification extends MessageTray.Notification {
    constructor(params) {
        // Extract expire-timeout-ms before passing params to super
        // (it's not a GObject property, so we need to handle it separately)
        const expireTimeoutMs = params['expire-timeout-ms'] || 0;
        const { ['expire-timeout-ms']: _, ...cleanParams } = params;

        super(cleanParams);

        this._actionLayout = params['action-layout'] || 'horizontal';
        this._verticalButtonBox = null;
        this._countdownIndicator = null;
        this._countdownTimeoutId = null;
        this._expireTimeoutMs = expireTimeoutMs;

        // Connect to destroy signal for cleanup (GNOME 49)
        this.connect('destroy', this._onDestroy.bind(this));
    }

    _onDestroy() {
        this._stopCountdown();
    }

    /**
     * Create banner content - override to add custom button layout
     */
    createBanner() {
        // Call parent to create basic banner
        const banner = super.createBanner();

        // Add countdown indicator if expiring
        if (this._expireTimeoutMs > 0) {
            this._setupCountdownIndicator(banner);
        }

        // If we have many actions, use vertical layout
        if (this._actionLayout === 'vertical' && this._actions && this._actions.length > 0) {
            this._setupVerticalButtons(banner);
        }

        return banner;
    }

    /**
     * Setup countdown indicator
     */
    _setupCountdownIndicator(banner) {
        const indicator = new CountdownIndicator({
            radius: 10,
            x_align: Clutter.ActorAlign.START,
            y_align: Clutter.ActorAlign.CENTER,
            margin_left: 8,
        });

        // Add to banner using public API only
        banner.add_child(indicator);

        this._countdownIndicator = indicator;

        // Ensure countdown stops when banner is destroyed
        banner.connect('destroy', () => this._stopCountdown());

        // Start countdown
        this._countdownTimeoutId = indicator.startCountdown(this._expireTimeoutMs);
    }

    /**
     * Stop countdown (when notification is destroyed or action taken)
     */
    _stopCountdown() {
        if (this._countdownTimeoutId) {
            this._countdownIndicator.stopCountdown(this._countdownTimeoutId);
            this._countdownTimeoutId = null;
        }
    }

    /**
     * Set up vertical button layout
     */
    _setupVerticalButtons(banner) {
        // Don't try to hide default buttons - they won't be added since we override createBanner

        // Create vertical button container
        const verticalBox = new St.BoxLayout({
            vertical: true,
            style_class: 'ai-notification-button-container',
            x_align: Clutter.ActorAlign.END,
            y_align: Clutter.ActorAlign.CENTER,
            margin_top: 8,
            spacing: 4,
        });

        // Add buttons vertically
        if (this._actions) {
            for (let i = 0; i < this._actions.length; i++) {
                const action = this._actions[i];
                const button = this._createVerticalButton(action.label, i);
                verticalBox.add_child(button);
            }
        }

        // Add to banner (not banner.actor)
        banner.add_child(verticalBox);
        this._verticalButtonBox = verticalBox;
    }

    /**
     * Create a single vertical action button
     */
    _createVerticalButton(label, index) {
        const button = new St.Button({
            label: label,
            style_class: 'ai-notification-button',
            x_align: Clutter.ActorAlign.FILL,
            y_align: Clutter.ActorAlign.CENTER,
            can_focus: true,
        });

        button.connect('clicked', () => {
            this._emitAction(index);
        });

        return button;
    }

    /**
     * Emit action signal
     */
    _emitAction(index) {
        // Stop countdown when action is taken
        this._stopCountdown();

        if (this._actions && this._actions[index]) {
            const action = this._actions[index];
            if (action.callback) {
                action.callback();
            }
        }
    }
});
