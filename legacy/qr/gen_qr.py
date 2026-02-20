import qrcode
from qrcode.constants import ERROR_CORRECT_L

uri = "sgnl://linkdevice?uuid=w9zAbMwOC_5LPdAYAbK8_w%3D%3D&pub_key=BRsOWLUJKnrqxUZQqOug4fOj9k6fXrEYdRhS4KJThCQu"

# Create QR with explicit settings
qr = qrcode.QRCode(
    version=1,
    error_correction=ERROR_CORRECT_L,
    box_size=10,
    border=4,
)
qr.add_data(uri)
qr.make(fit=True)

# Create image with white background and black QR code
img = qr.make_image(fill_color="black", back_color="white")
img.save("qr/bot_link.png")
print(f"QR saved to qr/bot_link.png ({img.size[0]}x{img.size[1]} px)")
