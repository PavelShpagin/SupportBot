import cv2
from pyzbar.pyzbar import decode
import sys

def decode_qr(image_path):
    try:
        img = cv2.imread(image_path)
        if img is None:
            print(f"Error: Could not read image {image_path}")
            return

        decoded_objects = decode(img)
        if not decoded_objects:
            print("No QR code found in the image.")
            return

        for obj in decoded_objects:
            print(f"Found QR Code: {obj.data.decode('utf-8')}")
            # Also print the type
            print(f"Type: {obj.type}")
            
    except Exception as e:
        print(f"Error decoding QR: {e}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python decode_qr.py <image_path>")
        sys.exit(1)
    
    decode_qr(sys.argv[1])
