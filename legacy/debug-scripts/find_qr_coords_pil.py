
from PIL import Image
import sys

def find_qr_bbox(image_path):
    try:
        img = Image.open(image_path)
        img = img.convert("L")  # Convert to grayscale
        width, height = img.size
        
        # Threshold to binary (black/white)
        threshold = 200
        pixels = img.load()
        
        min_x, min_y = width, height
        max_x, max_y = 0, 0
        
        found_pixels = False
        
        # Scan the center area where the QR code is expected
        # Center is roughly (512, 384)
        scan_margin = 200
        scan_x_start = max(0, width // 2 - scan_margin)
        scan_x_end = min(width, width // 2 + scan_margin)
        scan_y_start = max(0, height // 2 - scan_margin)
        scan_y_end = min(height, height // 2 + scan_margin)
        
        for y in range(scan_y_start, scan_y_end):
            for x in range(scan_x_start, scan_x_end):
                if pixels[x, y] < threshold:  # Dark pixel
                    min_x = min(min_x, x)
                    min_y = min(min_y, y)
                    max_x = max(max_x, x)
                    max_y = max(max_y, y)
                    found_pixels = True
        
        if found_pixels:
            w = max_x - min_x
            h = max_y - min_y
            center_x = min_x + w // 2
            center_y = min_y + h // 2
            
            print(f"Found content bounding box:")
            print(f"  x: {min_x}")
            print(f"  y: {min_y}")
            print(f"  width: {w}")
            print(f"  height: {h}")
            print(f"  center: ({center_x}, {center_y})")
            
            # Suggest crop coordinates
            # We want a 300x300 crop centered on the QR code
            crop_w, crop_h = 300, 300
            crop_x = center_x - crop_w // 2
            crop_y = center_y - crop_h // 2
            
            print(f"\nSuggested crop coordinates (300x300):")
            print(f"  x: {crop_x}")
            print(f"  y: {crop_y}")
            print(f"  geometry: {crop_w}x{crop_h}+{crop_x}+{crop_y}")
            
        else:
            print("No dark pixels found in the center area.")
            
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    if len(sys.argv) > 1:
        find_qr_bbox(sys.argv[1])
    else:
        find_qr_bbox("full_screenshot.png")
