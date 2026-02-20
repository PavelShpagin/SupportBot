import qrcode
uri = "sgnl://linkdevice?uuid=aFAn0l84woOj-Q58ICBa9A%3D%3D&pub_key=BR1YaamRPNhpB6hqsn0uqmI6XjsQIuOMW4n6v60LAT8s"
qr = qrcode.QRCode(version=1, box_size=10, border=4)
qr.add_data(uri)
qr.make(fit=True)
img = qr.make_image(fill_color="black", back_color="white")
img.save("/tmp/signal_link_qr.png")
print("QR saved to /tmp/signal_link_qr.png")
