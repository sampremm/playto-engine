FROM node:20-alpine

WORKDIR /app

# Install dependencies first (better caching)
COPY package*.json ./
RUN npm install

# Copy everything else
COPY . .

# Ensure the .vite-temp directory exists and is writable
RUN mkdir -p node_modules/.vite-temp && chmod -R 777 node_modules

EXPOSE 5173

# Use --host to allow external connections
CMD ["npm", "run", "dev", "--", "--host", "0.0.0.0"]
