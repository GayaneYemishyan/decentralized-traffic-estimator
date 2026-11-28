FROM golang:1.27.4 AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY *.go ./
RUN CGO_ENABLED=0 go build -o /node .

FROM gcr.io/distroless/static
COPY --from=build /node /node
ENTRYPOINT ["/node"]