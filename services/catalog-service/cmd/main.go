// cmd/main.go
package main

import (
    "catalog-service/internal/models"
    "catalog-service/internal/repository"
    "encoding/json"
    "fmt"
    "net/http"
    "strconv"

    "gorm.io/driver/postgres"
    "gorm.io/gorm"
)

func main() {
    // Подключение к PostgreSQL
    dsn := "host=postgres-catalog user=catalog_user password=catalog_pass dbname=catalog_db port=5432 sslmode=disable"
    db, err := gorm.Open(postgres.Open(dsn), &gorm.Config{})
    if err != nil {
        panic("failed to connect to database: " + err.Error())
    }

    // Автоматическая миграция (создаст таблицу products)
    db.AutoMigrate(&models.Product{})

    // Репозиторий
    productRepo := repository.NewProductRepository(db)

    // Обработчики
    http.HandleFunc("GET /health", healthHandler)
    http.HandleFunc("GET /products", getAllProducts(productRepo))
    http.HandleFunc("POST /products", createProduct(productRepo))
    http.HandleFunc("GET /products/{id}", getProductByID(productRepo))
    http.HandleFunc("PUT /products/{id}", updateProduct(productRepo))
    http.HandleFunc("DELETE /products/{id}", deleteProduct(productRepo))

    fmt.Println("Catalog Service running on :8082")
    http.ListenAndServe(":8082", nil)
}

// health check
func healthHandler(w http.ResponseWriter, r *http.Request) {
    w.Write([]byte("Catalog Service OK"))
}

// Обработчик получения всех товаров
func getAllProducts(repo *repository.ProductRepository) http.HandlerFunc {
    return func(w http.ResponseWriter, r *http.Request) {
        products, err := repo.FindAll()
        if err != nil {
            http.Error(w, err.Error(), http.StatusInternalServerError)
            return
        }
        w.Header().Set("Content-Type", "application/json")
        json.NewEncoder(w).Encode(products)
    }
}

// Обработчик создания товара
func createProduct(repo *repository.ProductRepository) http.HandlerFunc {
    return func(w http.ResponseWriter, r *http.Request) {
        var product models.Product
        if err := json.NewDecoder(r.Body).Decode(&product); err != nil {
            http.Error(w, "Invalid request body", http.StatusBadRequest)
            return
        }
        if err := repo.Create(&product); err != nil {
            http.Error(w, err.Error(), http.StatusInternalServerError)
            return
        }
        w.Header().Set("Content-Type", "application/json")
        w.WriteHeader(http.StatusCreated)
        json.NewEncoder(w).Encode(product)
    }
}

// Обработчик получения товара по ID
func getProductByID(repo *repository.ProductRepository) http.HandlerFunc {
    return func(w http.ResponseWriter, r *http.Request) {
        idStr := r.PathValue("id")
        id, err := strconv.Atoi(idStr)
        if err != nil {
            http.Error(w, "Invalid ID", http.StatusBadRequest)
            return
        }
        product, err := repo.FindByID(uint(id))
        if err != nil {
            http.Error(w, "Product not found", http.StatusNotFound)
            return
        }
        w.Header().Set("Content-Type", "application/json")
        json.NewEncoder(w).Encode(product)
    }
}

// Обработчик обновления товара
func updateProduct(repo *repository.ProductRepository) http.HandlerFunc {
    return func(w http.ResponseWriter, r *http.Request) {
        idStr := r.PathValue("id")
        id, err := strconv.Atoi(idStr)
        if err != nil {
            http.Error(w, "Invalid ID", http.StatusBadRequest)
            return
        }
        var updated models.Product
        if err := json.NewDecoder(r.Body).Decode(&updated); err != nil {
            http.Error(w, "Invalid request body", http.StatusBadRequest)
            return
        }
        product, err := repo.FindByID(uint(id))
        if err != nil {
            http.Error(w, "Product not found", http.StatusNotFound)
            return
        }
        product.Name = updated.Name
        product.Description = updated.Description
        product.Price = updated.Price
        product.Quantity = updated.Quantity
        if err := repo.Update(product); err != nil {
            http.Error(w, err.Error(), http.StatusInternalServerError)
            return
        }
        w.Header().Set("Content-Type", "application/json")
        json.NewEncoder(w).Encode(product)
    }
}

// Обработчик удаления товара
func deleteProduct(repo *repository.ProductRepository) http.HandlerFunc {
    return func(w http.ResponseWriter, r *http.Request) {
        idStr := r.PathValue("id")
        id, err := strconv.Atoi(idStr)
        if err != nil {
            http.Error(w, "Invalid ID", http.StatusBadRequest)
            return
        }
        if err := repo.Delete(uint(id)); err != nil {
            http.Error(w, "Product not found", http.StatusNotFound)
            return
        }
        w.WriteHeader(http.StatusNoContent)
    }
}

