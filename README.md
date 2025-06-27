# AllStarLink Interactive Map

An interactive web client built with MapLibre GL JS, deck.gl and a FastAPI backend.  
Displays AllStarLink nodes, shows detailed info on hover/click, and lets you perform actions like Monitor, Connect, Muted and Disconnect.


![Screenshot](images/screenshot.png)


## Features

- **User authentication** via FastAPI `/auth/token`
- **Interactive map** of nodes with MapLibre GL JS + deck.gl
- **Hover** to preview node info
- **Click** to lock info panel and view connections
- **Disconnect** button in home-node connections with auto-refresh
- **Home node** selection from dropdown

## Requirements

- Python 3.8+
- FastAPI & Uvicorn
- Modern web browser (Chrome, Firefox, Edge)

## Installation

1. **Clone the repo**  
   ```bash
   git clone https://github.com/emanuelelaface/allstarmap.git
   cd allstarmap
   ```

2. **Install Python dependencies**  
   ```bash
   pip install -r requirements.txt
   ```

## Configuration

Open `map/index.html` and set the API base URL:

```js
const API_BASE = 'http://<YOUR_FASTAPI_HOST>:8501';
```

Replace `<YOUR_FASTAPI_HOST>` with the hostname or IP where your FastAPI server runs.

## Running

Start the FastAPI server (serves both API and frontend):

```bash
python allstarapi.py
```

Then browse to:

```
http://<YOUR_FASTAPI_HOST>:8501/map
```
to use the default map gui, if you want to build your own interface you can exploit the FastAPI that is fully documented at the link:
```
http://<YOUR_FASTAPI_HOST>:8501/docs
```

