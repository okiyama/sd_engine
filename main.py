# Import necessary libraries
import pygame
import torch
from torch import autocast
from diffusers import StableDiffusionPipeline
import sys
import threading
import time
import numpy as np
from OpenGL.GL import *
from OpenGL.GLU import *
from pygame.locals import *
from PIL import Image
import queue  # For thread-safe communication

# Initialize Pygame and OpenGL
pygame.init()
display = (800, 600)
screen = pygame.display.set_mode(display, DOUBLEBUF | OPENGL)
pygame.display.set_caption('AI-Generated Game World')
clock = pygame.time.Clock()

# Check if CUDA is available
if torch.cuda.is_available():
    device = torch.device('cuda')
    print("using CUDA")
    dtype = torch.float16  # Use half-precision on GPU
else:
    device = torch.device('cpu')
    print("using CPU oh no!")
    dtype = torch.float32  # Use full precision on CPU

# Load the model
print("Loading Stable Diffusion model. This may take a few minutes...")
model = StableDiffusionPipeline.from_pretrained(
    'CompVis/stable-diffusion-v1-4',
    torch_dtype=dtype
).to(device)
print("Model loaded.")

# Enable attention slicing only if using CUDA
if device.type == 'cuda':
    model.enable_attention_slicing()

# Define the game map and prompts
# Define diverse prompts for each cell
grid_size = (5, 5)
grid_prompts = [
    ["A futuristic cityscape", "A serene beach", "A dense forest", "A snowy mountain", "A desert landscape"],
    ["An underwater coral reef", "A bustling medieval market", "An alien planet", "A magical floating castle", "A volcanic landscape"],
    ["A night sky with galaxies", "A sunflower field", "An abstract painting", "A futuristic spaceship interior", "A peaceful Japanese garden"],
    ["A haunted house on a hill", "A tropical rainforest", "A cyberpunk street scene", "An ice cave with crystals", "A grassy meadow with wildflowers"],
    ["An ancient temple in the jungle", "A futuristic laboratory", "A mystical swamp with glowing plants", "A grand library with endless shelves", "An open ocean with whales"]
]

grid = grid_prompts  # Assuming grid_size matches the grid_prompts dimensions

# Image cache to store generated images
image_cache = {}

# Function to combine prompts without exceeding token limit
def combine_prompts(prompts, weights, max_length=77):
    # Combine prompts based on their weights
    combined = ''
    for prompt, weight in zip(prompts, weights):
        if weight > 0:
            # Include weight in the prompt for emphasis (may not be fully supported)
            prompt_text = f"{prompt}"
            if combined:
                combined += ' | ' + prompt_text
            else:
                combined = prompt_text
            # Check if the tokenized length exceeds max_length
            if len(model.tokenizer.encode(combined)) > max_length:
                break
    return combined


# Player class with movement and view controls
class Player:
    def __init__(self, position):
        self.position = np.array(position, dtype=float)  # (x, y)
        self.angle = np.array([0.0, 0.0])  # (yaw, pitch)
        self.image = None  # Current view texture ID
        self.generating = False  # Flag to check if image is being generated
        self.last_position = None  # To check for position changes
        self.cache_key = None  # Key for the image cache

        # Queue to hold images generated by the worker thread
        self.image_queue = queue.Queue()

    def move(self, direction):
        self.position += direction
        # Clamp position within grid bounds
        self.position = np.clip(self.position, [0, 0], np.array(grid_size) - 1)

    def look(self, d_angle):
        self.angle += d_angle
        # Clamp pitch to avoid flipping
        self.angle[1] = np.clip(self.angle[1], -90, 90)

    def generate_image(self):
        if self.generating:
            return
        self.generating = True
        self.last_position = self.position.copy()

        # Compute cache key based on position
        self.cache_key = (int(self.position[0] * 10), int(self.position[1] * 10))

        if self.cache_key in image_cache:
            self.image = image_cache[self.cache_key]
            self.generating = False
            return

        # Interpolate between prompts
        x0, y0 = int(self.position[0]), int(self.position[1])
        x1, y1 = min(x0 + 1, grid_size[0] - 1), min(y0 + 1, grid_size[1] - 1)
        t_x = self.position[0] - x0
        t_y = self.position[1] - y0

        prompts = [
            grid[x0][y0],
            grid[x1][y0],
            grid[x0][y1],
            grid[x1][y1]
        ]

        weights = [ (1 - t_x) * (1 - t_y),
                    t_x * (1 - t_y),
                    (1 - t_x) * t_y,
                    t_x * t_y ]

        # Combine prompts based on weights
        combined_prompt = combine_prompts(prompts, weights)

        def run_generation():
            try:
                with torch.no_grad():
                    if device.type == 'cuda':
                        with autocast('cuda'):
                            # Generate image
                            images = model(
                                combined_prompt,
                                height=512,
                                width=512,
                                num_inference_steps=5
                            ).images
                    else:
                        # Generate image without autocast
                        images = model(
                            combined_prompt,
                            height=512,
                            width=512,
                            num_inference_steps=5
                        ).images

                    high_res = images[0]

                # Put the image in the queue to be processed in the main thread
                self.image_queue.put((self.cache_key, high_res))

            except Exception as e:
                print(f"Error during image generation: {e}")
            finally:
                self.generating = False

        threading.Thread(target=run_generation).start()

    def process_generated_image(self):
        # Check if there is an image to process
        try:
            cache_key, image = self.image_queue.get_nowait()
            # Convert PIL image to texture
            texture_id = self.image_from_pil(image)
            image_cache[cache_key] = texture_id
            self.image = texture_id
        except queue.Empty:
            pass

    def image_from_pil(self, image):
        import numpy as np  # Ensure numpy is imported as np
        image = image.transpose(method=Image.FLIP_TOP_BOTTOM)
        img_data = np.array(image.convert("RGBA"), dtype=np.uint8)
        width, height = image.size

        texture_id = glGenTextures(1)
        glBindTexture(GL_TEXTURE_2D, texture_id)

        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR)

        # Ensure img_data is contiguous in memory
        img_data = img_data.ctypes.data_as(ctypes.POINTER(ctypes.c_ubyte))

        glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, width, height, 0, GL_RGBA, GL_UNSIGNED_BYTE, img_data)

        return texture_id

    def draw(self):
        if not self.image:
            return

        glClear(GL_COLOR_BUFFER_BIT | GL_DEPTH_BUFFER_BIT)
        glLoadIdentity()
        gluLookAt(0, 0, 0, 0, 0, -1, 0, 1, 0)

        # Apply camera rotations
        glRotatef(-self.angle[1], 1, 0, 0)
        glRotatef(-self.angle[0], 0, 1, 0)

        glEnable(GL_TEXTURE_2D)
        glBindTexture(GL_TEXTURE_2D, self.image)

        # Custom sphere rendering
        self.render_sphere(radius=50, slices=64, stacks=64)

        glDisable(GL_TEXTURE_2D)

    def render_sphere(self, radius, slices, stacks):
        for i in range(stacks):
            lat0 = np.pi * (-0.5 + float(i) / stacks)
            z0 = np.sin(lat0) * radius
            zr0 = np.cos(lat0) * radius

            lat1 = np.pi * (-0.5 + float(i + 1) / stacks)
            z1 = np.sin(lat1) * radius
            zr1 = np.cos(lat1) * radius

            glBegin(GL_QUAD_STRIP)
            for j in range(slices + 1):
                lng = 2 * np.pi * float(j) / slices
                x = np.cos(lng)
                y = np.sin(lng)

                # Texture coordinates
                u = float(j) / slices
                v0 = float(i) / stacks
                v1 = float(i + 1) / stacks

                glTexCoord2f(u, v0)
                glVertex3f(x * zr0, y * zr0, z0)
                glTexCoord2f(u, v1)
                glVertex3f(x * zr1, y * zr1, z1)
            glEnd()


# Function to initialize OpenGL settings
def init_opengl():
    glEnable(GL_DEPTH_TEST)
    glDepthFunc(GL_LEQUAL)
    glClearColor(0, 0, 0, 1)
    glEnable(GL_TEXTURE_2D)

    # Set up projection
    glMatrixMode(GL_PROJECTION)
    glLoadIdentity()
    gluPerspective(60, display[0]/display[1], 0.1, 1000.0)
    glMatrixMode(GL_MODELVIEW)

    glDisable(GL_CULL_FACE)
# Main game loop
def main():
    init_opengl()
    player = Player((2.5, 2.5))
    player.generate_image()  # Generate initial image

    running = True
    mouse_sensitivity = 0.1
    move_speed = 0.05

    # Hide the mouse cursor and center it
    pygame.mouse.set_visible(False)
    pygame.event.set_grab(True)

    while running:
        dt = clock.tick(60) / 1000  # Delta time in seconds

        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
                sys.exit()

        keys = pygame.key.get_pressed()
        direction = np.array([0.0, 0.0])
        if keys[pygame.K_w]:
            direction[1] -= move_speed
        if keys[pygame.K_s]:
            direction[1] += move_speed
        if keys[pygame.K_a]:
            direction[0] -= move_speed
        if keys[pygame.K_d]:
            direction[0] += move_speed

        if np.any(direction):
            # Rotate movement direction based on player's yaw
            yaw_rad = np.radians(player.angle[0])
            cos_yaw = np.cos(yaw_rad)
            sin_yaw = np.sin(yaw_rad)
            dx = direction[0] * cos_yaw - direction[1] * sin_yaw
            dy = direction[0] * sin_yaw + direction[1] * cos_yaw
            player.move([dx, dy])

            # Generate new image if the position has changed significantly
            if player.last_position is None or np.linalg.norm(player.position - player.last_position) > 0.1:
                player.generate_image()

        # Mouse look
        mouse_dx, mouse_dy = pygame.mouse.get_rel()
        player.look(np.array([mouse_dx * mouse_sensitivity, mouse_dy * mouse_sensitivity]))

        # Process any images generated by the worker thread
        player.process_generated_image()

        # Draw the scene
        player.draw()

        pygame.display.flip()

    # Cleanup
    pygame.quit()
    torch.cuda.empty_cache()

if __name__ == "__main__":
    main()
