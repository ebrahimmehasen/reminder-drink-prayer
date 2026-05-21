# Hydration and Prayer Bot 💧🕌

A comprehensive automation bot designed to help you stay hydrated and ensure you never miss your prayer times, while seamlessly integrating with your daily workflow.

## Features ✨
- **Smart Reminders**: Automated hydration and prayer reminders.
- **Focus / Prayer Time Freeze**: Initiates a 10-minute full-screen "freeze" that blocks UI interaction during prayer times to encourage taking a break, preventing system sleep or hibernation during this period.
- **Dynamic UI**: A dynamically positioned and resized user interface that adapts to your screen.
- **Offline Mode**: Caches prayer times locally to ensure continuous and reliable functionality even during internet outages.
- **WhatsApp Automation**: Automatically sends WhatsApp messages (via Microsoft Edge) for prayer reminders to keep you and your contacts notified.
- **Silent & Automatic Startup**: Runs completely silently in the background with zero terminal windows. Automatically launches with Windows startup (via `start_bot.vbs`).

## Requirements 🛠️
- Python 3.x
- Microsoft Edge (for WhatsApp automation features)
- Required Python packages (see imports in the source code)
- Windows OS (for `start_bot.vbs` startup automation)

## Setup & Installation 🚀
1. Clone the repository to your local machine:
   ```bash
   git clone https://github.com/yourusername/hydration-prayer-bot.git
   ```
2. Navigate to the project directory:
   ```bash
   cd hydration-prayer-bot
   ```
3. Install the required dependencies using pip.
4. Update `config.yaml` with your preferred settings (if applicable).

## How to Run 💻
### 1. Silent Background Mode (Recommended)
Simply double-click the `start_bot.vbs` file. This will launch the bot silently in the background using `pythonw.exe`. 
*Note: This script is already configured to be copied to your Windows Startup folder to run automatically every time you turn on your laptop.*

### 2. Standard Mode (With Console)
If you want to see the logs and console output for debugging:
```bash
python hydration_prayer_bot.py
```

## How to Stop the Bot 🛑
Since the bot runs silently in the background, there is no window to close. To stop the bot:
1. Press `Ctrl + Shift + Esc` to open the **Task Manager**.
2. Go to the **Details** or **Processes** tab.
3. Look for `pythonw.exe` (or `Python`), right-click it, and select **End Task**.

## License 📜
This project is open-source and available under the [MIT License](LICENSE).
