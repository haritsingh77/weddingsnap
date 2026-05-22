import face_recognition
from PIL import Image, ImageDraw, ImageOps
import sys
import numpy as np
from pathlib import Path

def verify_photo(image_path):
    print(f"Checking: {image_path}")
    
    # Load and fix rotation
    img = Image.open(image_path).convert("RGB")
    img = ImageOps.exif_transpose(img)
    image = np.array(img)
    
    # Find all face locations
    face_locations = face_recognition.face_locations(image)
    print(f"Found {len(face_locations)} faces in this photo.")

    # Convert to PIL image to draw on it
    pil_image = Image.fromarray(image)
    draw = ImageDraw.Draw(pil_image)

    for (top, right, bottom, left) in face_locations:
        # Draw a box around the face
        draw.rectangle(((left, top), (right, bottom)), outline=(255, 0, 0), width=5)

    # Save the result
    output_path = "test_result.jpg"
    pil_image.save(output_path)
    print(f"✅ Result saved to: {output_path}")
    print("Open this file to see the red boxes around detected faces.")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python scripts/verify_faces.py /path/to/photo.jpg")
    else:
        verify_photo(sys.argv[1])
