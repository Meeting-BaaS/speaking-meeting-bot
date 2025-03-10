#!/usr/bin/env python3
"""
Example of using the Speaking Meeting Bot API.

This script demonstrates how to use the API to create a persona,
generate an image for it, and create a bot that joins a meeting.
"""
import os
import time
import requests
import json
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# API base URL - change this to match your deployment
API_BASE_URL = "http://localhost:8000"


def print_response(response):
    """Pretty print API response"""
    try:
        print(json.dumps(response.json(), indent=2))
        print("-" * 50)
    except Exception as e:
        print(f"Error parsing response: {e}")
        print(response.text)
        print("-" * 50)


def create_persona():
    """Create a new persona"""
    print("\n=== Creating Persona ===")
    
    # Define the persona
    persona_data = {
        "key": "api_created_bot",
        "name": "API Created Bot",
        "prompt": "You are a bot created via the API. You are helpful, friendly, and concise.",
        "entry_message": "Hello! I was created via the API and I'm here to help.",
        "characteristics": [
            "Helpful",
            "Friendly",
            "Concise",
            "API-created"
        ],
        "tone_of_voice": [
            "Professional",
            "Friendly",
            "Clear"
        ],
        "gender": "FEMALE"
    }
    
    # Send request to create persona
    response = requests.post(
        f"{API_BASE_URL}/personas",
        json=persona_data
    )
    
    print_response(response)
    return persona_data["key"]


def generate_image(persona_key):
    """Generate an image for the persona"""
    print("\n=== Generating Image ===")
    
    # Get API keys from environment
    replicate_key = os.getenv("REPLICATE_KEY")
    utfs_key = os.getenv("UTFS_KEY")
    app_id = os.getenv("APP_ID")
    
    if not all([replicate_key, utfs_key, app_id]):
        print("Missing API keys for image generation - skipping")
        return
    
    # Send request to generate image
    response = requests.post(
        f"{API_BASE_URL}/personas/{persona_key}/generate-image",
        json={
            "persona_key": persona_key,
            "replicate_key": replicate_key,
            "utfs_key": utfs_key,
            "app_id": app_id
        }
    )
    
    print_response(response)


def list_personas():
    """List all personas"""
    print("\n=== Listing Personas ===")
    
    response = requests.get(f"{API_BASE_URL}/personas")
    print_response(response)


def create_bot(persona_key, meeting_url, ngrok_url=None):
    """Create a bot with the persona"""
    print("\n=== Creating Bot ===")
    
    # Prepare request payload
    payload = {
        "meeting_url": meeting_url,
        "persona_key": persona_key,
    }
    
    # Add ngrok_url if provided
    if ngrok_url:
        payload["ngrok_url"] = ngrok_url
    
    # Send request to create bot
    response = requests.post(
        f"{API_BASE_URL}/bots",
        json=payload
    )
    
    print_response(response)
    
    if response.status_code == 200 and response.json().get("success"):
        return response.json()["data"]["bot_id"]
    return None


def list_bots():
    """List all active bots"""
    print("\n=== Listing Bots ===")
    
    response = requests.get(f"{API_BASE_URL}/bots")
    print_response(response)


def delete_bot(bot_id):
    """Delete a bot"""
    print(f"\n=== Deleting Bot {bot_id} ===")
    
    response = requests.delete(f"{API_BASE_URL}/bots/{bot_id}")
    print_response(response)


def main():
    """Main function"""
    print("Speaking Meeting Bot API Example")
    
    # Check if API is running
    try:
        response = requests.get(f"{API_BASE_URL}/health")
        if response.status_code != 200:
            print(f"API server not responding correctly: {response.status_code}")
            return
    except requests.exceptions.ConnectionError:
        print(f"Could not connect to API server at {API_BASE_URL}")
        print("Make sure the API server is running with: poetry run api")
        return
    
    # Get meeting URL from user
    meeting_url = input("Enter meeting URL (must start with https://): ")
    if not meeting_url.startswith("https://"):
        print("Meeting URL must start with https://")
        return
    
    # Ask if user wants to provide a custom ngrok URL or let the API create one
    use_custom_ngrok = input("Do you want to provide a custom ngrok URL? (y/n): ").lower() == 'y'
    ngrok_url = None
    
    if use_custom_ngrok:
        ngrok_url = input("Enter ngrok URL (must start with https://): ")
        if not ngrok_url.startswith("https://"):
            print("Ngrok URL must start with https://")
            return
    else:
        print("The API will create an ngrok tunnel automatically.")
    
    # Create persona
    persona_key = create_persona()
    
    # Generate image for persona
    generate_image(persona_key)
    
    # List personas
    list_personas()
    
    # Create bot
    bot_id = create_bot(persona_key, meeting_url, ngrok_url)
    if not bot_id:
        print("Failed to create bot")
        return
    
    # List bots
    list_bots()
    
    # Wait for user to terminate bot
    print("\nBot is running. Press Enter to delete bot and exit.")
    input()
    
    # Delete bot
    delete_bot(bot_id)


if __name__ == "__main__":
    main() 