import qrcode
uri = "sgnl://linkdevice?uuid=xXbStHjCgm69U9XHu6KmBQ%3D%3D&pub_key=BQm8Ys8yk8L5LyQIjIQuopReKGg1lQ0WZlOe%2FWDr2OY5"
qr = qrcode.make(uri)
qr.save("/tmp/bot_link_qr.png")
print("QR saved to /tmp/bot_link_qr.png")
