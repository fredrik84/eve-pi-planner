FROM mcr.microsoft.com/playwright:v1.63.0-noble

WORKDIR /work
COPY package.json package-lock.json ./
RUN npm ci --ignore-scripts

COPY playwright.config.js ./
COPY tests ./tests

CMD ["npm", "test"]
