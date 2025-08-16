# PHP + Apache
FROM php:8.2-apache

# Opcional: extensões mínimas (comente se não precisar)
RUN docker-php-ext-install pdo pdo_mysql

# Habilita rewrite (seguro mesmo sem .htaccess)
RUN a2enmod rewrite

# Evita quebra por build-arg ausente em alguns painéis
ARG GIT_SHA=local
LABEL git_sha=$GIT_SHA

# Copia app
WORKDIR /var/www/html
COPY . /var/www/html

# Permissões
RUN chown -R www-data:www-data /var/www/html

EXPOSE 80
CMD ["apache2-foreground"]