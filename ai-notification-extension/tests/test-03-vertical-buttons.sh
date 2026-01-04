#!/bin/bash
# test-03-vertical-buttons.sh

set -e

echo "=== Test 3: Vertical Button Layout ==="

CLIENT="/data/sync/work/leangeeks-ai/ai-playground/ai-notification-extension/client/notify-interactive"

# Test 3.1: Four options (should use vertical)
echo "3.1: Four vertical options..."
$CLIENT "Choose Background" \
    "How should the background be displayed?" \
    --layout vertical \
    --action white:"White background" \
    --action transparent:"Transparent background" \
    --action inherit:"Inherit from parent" \
    --action gradient:"Gradient fill"

sleep 1

# Test 3.2: Five options
echo "3.2: Five vertical options..."
$CLIENT "Color Scheme" \
    "Select your preferred color scheme:" \
    --layout vertical \
    --action light:"Light mode" \
    --action dark:"Dark mode" \
    --action auto:"Auto (system preference)" \
    --action sepia:"Sepia tone" \
    --action grayscale:"Grayscale"

sleep 1

echo "Vertical button tests passed"
