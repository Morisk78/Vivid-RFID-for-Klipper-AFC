# BTT-Vivid-RFID-Standalone-for-Klipper-AFC
This project provides a standalone RFID integration for the BTT Vivid in Klipper.

👉 RFID only
👉 No MMS motion control
👉 No slot handling
👉 Fully compatible with AFC (Automatic Filament Changer)

Purpose:

Detect/write/read RFID Tags only !!! MIFARE Classic 1K/4K (S50/S70) Tags
Output directly to Mainsail console
Base for automatic filament recognition
⚠️ Important

This setup is intentionally minimal:

❌ No MMS movement
❌ No slot logic
❌ No AFC conflicts
✅ Requirements
Klipper,Mainsail installed
BTT Vivid ( flashed with newer Klipper Firmware)
MFRC522 RFID reader
AFC system (optional)
MIFARE Classic 1K/4K (S50/S70) Tags

❗ Not supported:

NTAG213 / NTAG215 / NTAG216
MIFARE Ultralight

📦 Installation

1. Clone repository
   cd ~
   git clone https://github.com/Morisk78/BTT-Vivid-RFID-Standalone-f-r-Klipper-AFC-
   .git

3. Copy MMS folder cp -r vivid-rfid/mms ~home/pi/klipper/klippy/extras

4. Copy config  
   cp vivid-rfid/config/vivid_rfid.cfg ~home/pi/printer_data/config/

   cp vivid-rfid/config/rfid_macros.cfg ~home/pi/printer_data/config/

6. Restart Klipper
   sudo service klipper restart

8. Include in printer.cfg
   [include vivid_rfid.cfg]
   [include rfid_macros.cfg]

📂 Structure

   home/pi/klipper/klippy/extras/mms

   home/printer_data/config/vivid_rfid.cfg, rfid_macros.cfg



   <img width="332" height="243" alt="Screenshot 2026-03-25 223735" src="https://github.com/user-attachments/assets/04185b98-f317-4c0e-b6c8-59d6575c2607" />

   <img width="755" height="308" alt="image" src="https://github.com/user-attachments/assets/e34d05c2-9a90-4c4e-bede-6e11d5f7a477" />



🧪 Test Commands

   Start detect:

   MMS_RFID_DETECT_DEV NAME=mfrc522_0 SWITCH=1

   MMS_RFID_DETECT_DEV NAME=mfrc522_1 SWITCH=1

   Start wirte:

   MMS_RFID_WIRTE_DEV NAME=mfrc255_0

   MMS_RFID_WIRTE_DEV NAME=mfrc255_1

   Start read:

   MMS_RFID_READ_DEV NAME=mfrc522_0 SWITCH=1                 

   MMS_RFID_READ_DEV NAME=mfrc522_1 SWITCH=1

  Stop:

  📟 Expected Output

  No tag:   No Tag, return 

  With tag:     RFID[mfrc522_0] detect Tag uid: XXXXXXXX
                RFID[mfrc522_0] read data: {...}



