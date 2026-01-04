import GLib from 'gi://GLib';

/**
 * Text formatter for notifications
 * Handles code blocks, truncation, and Pango markup
 */
export class TextFormatter {
    constructor(options = {}) {
        this.maxLines = options.maxLines || 0; // 0 = no limit
        this.maxChars = options.maxChars || 500; // Soft limit
        this.codeStyle = options.codeStyle || 'pango'; // 'pango' or 'plain'
    }

    /**
     * Format notification body with code blocks
     * @param {string} body - Main body text
     * @param {string[]} codeBlocks - Array of code strings
     * @param {object} options - Formatting options
     * @returns {string} Formatted body
     */
    formatBody(body, codeBlocks = [], options = {}) {
        const maxLines = options.maxLines ?? this.maxLines;
        const truncate = options.truncate ?? true;

        let formatted = this._escapeText(body);

        // Add code blocks
        if (codeBlocks && codeBlocks.length > 0) {
            formatted += '\n\n';
            for (let i = 0; i < codeBlocks.length; i++) {
                const code = this._formatCodeBlock(codeBlocks[i], i);
                formatted += code;
            }
        }

        // Truncate if needed
        if (truncate && maxLines > 0) {
            formatted = this._truncateToLines(formatted, maxLines);
        }

        return formatted;
    }

    /**
     * Format a single code block
     */
    _formatCodeBlock(code, index = 0) {
        // Trim leading/trailing whitespace but preserve indentation
        const trimmed = code.trim();

        // Use Pango monospace tag
        if (this.codeStyle === 'pango') {
            // Escape the code content
            const escaped = this._escapeText(trimmed);
            return `<tt>${escaped}</tt>\n`;
        } else {
            // Plain text with simple formatting
            return `┌─ Code ${index + 1} ─${'─'.repeat(Math.max(0, 20 - trimmed.length))}\n` +
                   `${trimmed}\n` +
                   `└${'─'.repeat(Math.min(50, trimmed.length + 15))}\n`;
        }
    }

    /**
     * Truncate text to max lines, adding "..." indicator
     */
    _truncateToLines(text, maxLines) {
        const lines = text.split('\n');

        if (lines.length <= maxLines) {
            return text;
        }

        // Count visual lines (accounting for wrapping)
        let visualLines = 0;
        let truncateIndex = -1;

        for (let i = 0; i < lines.length; i++) {
            // Approximate line wrapping (roughly 50 chars per line)
            const wrappedLines = Math.ceil(lines[i].length / 50);
            visualLines += wrappedLines;

            if (visualLines > maxLines) {
                truncateIndex = i;
                break;
            }
        }

        if (truncateIndex >= 0) {
            const truncated = lines.slice(0, truncateIndex).join('\n');
            return truncated + '\n\n… (click to see more)';
        }

        return text;
    }

    /**
     * Escape text for Pango markup
     */
    _escapeText(text) {
        if (!text) return '';

        return text
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&apos;');
    }

    /**
     * Create a code block from command output
     * @param {string} command - Command string
     * @param {string} output - Command output
     * @returns {string} Formatted code block
     */
    formatCommand(command, output) {
        const formatted = `$ ${command}\n${output}`;
        return this._formatCodeBlock(formatted);
    }

    /**
     * Format a diff/code snippet
     * @param {string} diff - Diff content
     * @param {string} language - Optional language hint
     * @returns {string} Formatted code block
     */
    formatDiff(diff, language = 'diff') {
        return this._formatCodeBlock(diff);
    }

    /**
     * Get formatted code blocks from a multi-line string
     * @param {string} text - Text that may contain code blocks
     * @param {string} delimiter - Code block delimiter (default: ```)
     * @returns {object} { body, codeBlocks }
     */
    parseCodeBlocks(text, delimiter = '```') {
        const lines = text.split('\n');
        const bodyLines = [];
        const codeBlocks = [];
        let inCodeBlock = false;
        let currentCode = [];

        for (const line of lines) {
            if (line.trim().startsWith(delimiter)) {
                inCodeBlock = !inCodeBlock;
                if (!inCodeBlock && currentCode.length > 0) {
                    codeBlocks.push(currentCode.join('\n'));
                    currentCode = [];
                }
            } else if (inCodeBlock) {
                currentCode.push(line);
            } else {
                bodyLines.push(line);
            }
        }

        // Handle unclosed code block
        if (currentCode.length > 0) {
            codeBlocks.push(currentCode.join('\n'));
        }

        return {
            body: bodyLines.join('\n'),
            codeBlocks,
        };
    }
}
