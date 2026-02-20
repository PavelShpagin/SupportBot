import sys
import cv2
from pyzbar.pyzbar import decode
from PIL import Image

def check_qr(image_path):
    try:
        img = Image.open(image_path)
        decoded_objects = decode(img)
        if decoded_objects:
            for obj in decoded_objects:
                print(f"Decoded QR: {obj.data.decode('utf-8')}")
            return True
        else:
            print("No QR code found.")
            return False
    except Exception as e:
        print(f"Error: {e}")
        return False

if __name__ == "__main__":
    if len(sys.argv) > 1:
        check_qr(sys.argv[1])
    else:
        print("Usage: python check_qr.py <image_path>")
